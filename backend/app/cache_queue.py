"""Atomic SHA-256 upload deduplication in Redis (one job per cache key).

The Redis script ensures that, across concurrent API containers, exactly one
request becomes the processing owner. Each upload still has its own UUID.
"""
import json
import logging
from enum import IntEnum

from redis.asyncio import Redis

from app.cache import active_key, result_key
from app.errors import EnqueueOutcomeUnknown
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.storage import StagedFile
from app.observability import inject, current_ids

log = logging.getLogger(__name__)


class CacheOutcome(IntEnum):
    OWNER = 0
    SHARED = 1
    HIT = 2


_REGISTER = """
-- cache:register
-- KEYS: result, active, meta, pages, summary, stream
-- ARGV: metadata JSON, doc ID, parser, result TTL, active TTL, fingerprint, metadata prefix
local metadata = cjson.decode(ARGV[1])
local function write_meta(fields)
    local values = {}
    for k,v in pairs(fields) do
        table.insert(values, k)
        table.insert(values, tostring(v))
    end
    redis.call('HSET', KEYS[3], unpack(values))
end
if redis.call('EXISTS', KEYS[3]) ~= 0 then
    return redis.error_reply('Duplicate upload document ID')
end
local saved = redis.call('GET', KEYS[1])
if saved then
    local snapshot = cjson.decode(saved)
    if snapshot.pages and snapshot.summary and snapshot.page_count then
        metadata.status = 'completed'
        metadata.page_count = tostring(snapshot.page_count)
        metadata.cache = 'hit'
        if metadata.archive_document_id then
            metadata.status = 'queued'
            metadata.cache_source_id = snapshot.source_run_id or ''
        end
        write_meta(metadata)
        redis.call('SET', KEYS[4], snapshot.pages, 'EX', ARGV[4])
        redis.call('SET', KEYS[5], snapshot.summary, 'EX', ARGV[4])
        if metadata.archive_document_id then
            redis.call('XADD', KEYS[6], '*', 'document_id', ARGV[2], 'parser', ARGV[3], 'action', 'cache_hit', 'traceparent', ARGV[8], 'upload_id', ARGV[9])
        else
            redis.call('EXPIRE', KEYS[3], ARGV[4])
        end
        return 2
    end
end
local owner = redis.call('GET', KEYS[2])
if owner then
    local previous = ARGV[7] .. ':' .. owner .. ':meta'
    local state = redis.call('HGET', previous, 'status')
    if state == 'queued' or state == 'processing' or state == 'summarizing' then
        metadata.status = 'queued'
        metadata.cache = 'shared'
        metadata.cache_source_id = owner
        write_meta(metadata)
        if metadata.archive_document_id then
            redis.call('XADD', KEYS[6], '*', 'document_id', ARGV[2], 'parser', ARGV[3], 'action', 'shared', 'traceparent', ARGV[8], 'upload_id', ARGV[9])
        else
            redis.call('EXPIRE', KEYS[3], ARGV[5])
        end
        return 1
    end
end
metadata.cache = 'miss'
metadata.cache_key = ARGV[6]
write_meta(metadata)
redis.call('XADD', KEYS[6], '*', 'document_id', ARGV[2], 'parser', ARGV[3], 'traceparent', ARGV[8], 'upload_id', ARGV[9])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[5])
return 0
"""


async def register_upload(redis: Redis, queue: DocumentQueue, repo: DocumentRepository,
                          doc: StagedFile, digest: str, *, cache_ttl: int, durable: bool = False) -> CacheOutcome:
    """Call only after this UUID's PDF has reached S3.

    A lost Redis EVAL response is an uncertain outcome. Caller must preserve
    the source object (the lifecycle rule is its fallback cleanup).
    """
    metadata = repo.prepare_meta(doc.id, doc.filename, doc.parser.value)
    if durable:
        metadata["archive_document_id"] = doc.sha256
    trace_id, upload_id = current_ids()
    if trace_id: metadata["trace_id"] = trace_id
    if upload_id: metadata["upload_id"] = upload_id
    try:
        outcome = await redis.eval(
            _REGISTER, 6, result_key(digest), active_key(digest),
            repo.key(doc.id, 'meta'), repo.key(doc.id, 'pages'),
            repo.key(doc.id, 'summary'), queue.stream,
            json.dumps(metadata), str(doc.id), doc.parser.value,
            repo.result_ttl, cache_ttl, digest, repo.prefix, inject(), upload_id,
        )
        return CacheOutcome(int(outcome))
    except Exception as exc:
        log.error('Cache registration outcome uncertain for document %s', doc.id)
        raise EnqueueOutcomeUnknown() from exc
