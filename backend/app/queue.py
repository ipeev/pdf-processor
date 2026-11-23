"""Redis Streams jobs: consumer groups, abandoned-job claims and fenced ACKs."""
import logging
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.errors import EnqueueOutcomeUnknown, ProcessingError
from app.redis_store import DocumentRepository, verify_exec, now_utc
from app.storage import StagedFile
from app.observability import inject, current_ids

log = logging.getLogger(__name__)

# Redis executes these scripts atomically. Never trust a local timer or a stale
# worker's memory to decide whether it still owns a Stream message.
_BEGIN = """
-- queue:begin
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending ~= 1 or pending[1][2] ~= ARGV[3] then return 0 end
if redis.call('EXISTS', KEYS[2]) == 0 then return -1 end
redis.call('HSET', KEYS[2], 'owner_token', ARGV[4])
return 1
"""
_HEARTBEAT = """
-- queue:heartbeat
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending ~= 1 or pending[1][2] ~= ARGV[3] then return 0 end
local ids = redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2], 'JUSTID')
return #ids
"""
_ACK_OWNED = """
-- queue:ack_owned
if redis.call('HGET', KEYS[2], 'owner_token') ~= ARGV[4] then return 0 end
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending ~= 1 or pending[1][2] ~= ARGV[3] then return 0 end
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
if acknowledged == 1 then
    redis.call('XDEL', KEYS[1], ARGV[2])
    if redis.call('HEXISTS', KEYS[2], 'archive_document_id') == 1 then
        redis.call('HDEL', KEYS[2], 'owner_token')
    end
end
return acknowledged
"""


class DocumentQueue:
    def __init__(self, redis: Redis, stream: str, group: str, consumer: str):
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self._claim_cursor = "0-0"

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue_batch(self, docs: list[StagedFile], repo: DocumentRepository) -> None:
        pipe = self.redis.pipeline(transaction=True)
        for doc in docs:
            pipe.hset(repo.key(doc.id, "meta"), mapping=repo.prepare_meta(doc.id, doc.filename, doc.parser.value))
            pipe.xadd(self.stream, {"document_id": str(doc.id), "parser": doc.parser.value, "traceparent": inject(), "upload_id": current_ids()[1]})
        # No rollback on runtime command errors. Unknown outcome => do not delete PDFs.
        try:
            replies = await pipe.execute(raise_on_error=False)
            verify_exec(replies, len(docs) * 2)
        except Exception as exc:
            log.error("Upload commit outcome uncertain; source PDFs preserved. ids=%s", [str(d.id) for d in docs])
            raise EnqueueOutcomeUnknown() from exc

    async def enqueue_summary_retry(self, doc_id: UUID | str, repo: DocumentRepository) -> None:
        """Atomically accept exactly one retry for a partial result (Redis Lua)."""
        doc_id = UUID(str(doc_id))
        script = """
        local state = redis.call('HGET', KEYS[1], 'status')
        if not state then return 0 end
        if state ~= 'partial' then return 1 end
        if redis.call('EXISTS', KEYS[2]) == 0 then return 2 end
        local parser = redis.call('HGET', KEYS[1], 'parser')
        redis.call('XADD', KEYS[3], '*', 'document_id', ARGV[1], 'parser', parser, 'action', 'summary', 'traceparent', ARGV[3], 'upload_id', redis.call('HGET', KEYS[1], 'upload_id') or '')
        redis.call('HSET', KEYS[1], 'status', 'summarizing', 'updated_at', ARGV[2],
                   'error_code', '', 'error_message', '', 'owner_token', '', 'worker_attempts', '0')
        return 3
        """
        code = await self.redis.eval(script, 3, repo.key(doc_id, "meta"),
                                     repo.key(doc_id, "pages"), self.stream,
                                     str(doc_id), now_utc(), inject())
        if code == 0:
            raise ProcessingError("DOCUMENT_NOT_FOUND", "Document does not exist.", 404)
        if code == 1:
            raise ProcessingError("SUMMARY_RETRY_NOT_ALLOWED", "Only partial results can retry summarization.", 409)
        if code == 2:
            raise ProcessingError("RESULT_NOT_READY", "Extracted pages have expired or are missing.", 409)
        if code != 3:
            raise ProcessingError("REDIS_TRANSACTION_UNCERTAIN", "Unable to confirm summary retry.", 503)

    async def read(self, cursor: str, block_ms: int = 1000) -> list[tuple[str, dict[str, str]]]:
        response = await self.redis.xreadgroup(
            self.group, self.consumer, {self.stream: cursor}, count=1,
            block=block_ms if cursor == ">" else None,
        )
        return response[0][1] if response else []

    async def claim_abandoned(self, min_idle_ms: int) -> list[tuple[str, dict[str, str]]]:
        """Claim one message only after its worker stopped refreshing its idle clock."""
        next_cursor, entries, *_ = await self.redis.xautoclaim(
            self.stream, self.group, self.consumer,
            min_idle_time=min_idle_ms, start_id=self._claim_cursor, count=1,
        )
        # XAUTOCLAIM scans at most COUNT*10 pending IDs. Advance its cursor
        # or a large set of live/heartbeating jobs could starve older failures.
        self._claim_cursor = next_cursor
        return entries

    async def begin(self, message_id: str, doc_id: UUID, token: str, repo: DocumentRepository) -> bool:
        result = await self.redis.eval(_BEGIN, 2, self.stream, repo.key(doc_id, "meta"),
                                       self.group, message_id, self.consumer, token)
        if result == -1:
            raise ProcessingError("JOB_ORPHANED", "Job metadata is missing.")
        return result == 1

    async def heartbeat(self, message_id: str) -> bool:
        return bool(await self.redis.eval(_HEARTBEAT, 1, self.stream,
                                          self.group, message_id, self.consumer))

    async def ack_owned(self, message_id: str, doc_id: UUID, token: str,
                        repo: DocumentRepository) -> bool:
        return bool(await self.redis.eval(_ACK_OWNED, 2, self.stream, repo.key(doc_id, "meta"),
                                          self.group, message_id, self.consumer, token))

    async def ack(self, message_id: str) -> int:
        """Legacy/test helper; production worker uses ack_owned for fencing."""
        acked = await self.redis.xack(self.stream, self.group, message_id)
        if acked:
            await self.redis.xdel(self.stream, message_id)
        return acked
