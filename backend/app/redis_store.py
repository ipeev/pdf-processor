"""Redis metadata, retained extracted pages and summary; terminal results expire."""
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.cache import active_key, result_key
from app.errors import ProcessingError
from app.provider_diagnostics import sanitize_diagnostic
from app.parsers.base import ParsedDocument, ParsedPage
from app.schemas import PageProgress


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_exec(replies: list[Any], expected: int) -> None:
    if len(replies) != expected or any(isinstance(value, BaseException) for value in replies):
        raise ProcessingError("REDIS_TRANSACTION_UNCERTAIN", "Redis transaction did not complete consistently.", 503)


class DocumentRepository:
    def __init__(self, redis: Redis, prefix: str = "documents", *, result_ttl: int = 86400, cache_ttl: int = 86400):
        self.redis = redis
        self.prefix = prefix
        self.result_ttl = result_ttl
        self.cache_ttl = cache_ttl

    def key(self, doc_id: UUID | str, suffix: str) -> str:
        doc_id = UUID(str(doc_id))
        return f"{self.prefix}:{doc_id}:{suffix}"

    async def get_meta(self, doc_id: UUID | str) -> dict[str, str] | None:
        meta = await self.redis.hgetall(self.key(doc_id, "meta"))
        return meta or None

    @staticmethod
    def _decode_pages(raw: str | None, meta: dict) -> ParsedDocument:
        try:
            pages = json.loads(raw) if raw is not None else None
            expected = int(meta.get("page_count", "0"))
            if (expected <= 0 or not isinstance(pages, list) or len(pages) != expected
                    or any(not isinstance(p, dict) or p.get("page") != i
                           or not isinstance(p.get("markdown"), str)
                           for i, p in enumerate(pages, start=1))):
                raise ValueError("invalid page set")
            parsed = ParsedDocument([ParsedPage(p["page"], p["markdown"]) for p in pages])
            parsed.validate()
            return parsed
        except (ValueError, TypeError, json.JSONDecodeError, ProcessingError) as exc:
            raise ProcessingError("RESULT_CORRUPT", "Extracted pages are inconsistent or missing.", 503) from exc

    async def read_extracted(self, doc_id: UUID | str, meta: dict | None = None) -> ParsedDocument:
        meta = meta or await self.get_meta(doc_id)
        if not meta or meta.get("status") not in {"processing", "summarizing", "partial", "completed"}:
            raise ProcessingError("RESULT_NOT_READY", "Extracted pages are not available yet.", 409)
        raw = await self.redis.get(self.key(doc_id, "pages"))
        if raw is None:
            if meta.get("page_count"):
                raise ProcessingError("RESULT_CORRUPT", "Extracted pages are missing.", 503)
            raise ProcessingError("RESULT_NOT_READY", "Extracted pages are not available yet.", 409)
        return self._decode_pages(raw, meta)

    async def get_saved_extraction(self, doc_id: UUID | str) -> ParsedDocument | None:
        meta = await self.get_meta(doc_id)
        if not meta or not meta.get("page_count"):
            return None
        return await self.read_extracted(doc_id, meta)

    async def is_complete(self, doc_id: UUID | str, meta: dict | None = None) -> bool:
        meta = meta or await self.get_meta(doc_id)
        if meta is None or meta.get("status") != "completed":
            return False
        try:
            await self.read_completed(doc_id, meta)
        except ProcessingError:
            return False
        return True

    async def read_completed(self, doc_id: UUID | str, meta: dict | None = None) -> tuple[list[dict], str]:
        meta = meta or await self.get_meta(doc_id)
        if meta is None or meta.get("status") != "completed":
            raise ProcessingError("RESULT_NOT_READY", "Document processing is not complete.", 409)
        parsed = await self.read_extracted(doc_id, meta)
        summary = await self.redis.get(self.key(doc_id, "summary"))
        if not isinstance(summary, str) or not summary.strip():
            raise ProcessingError("RESULT_CORRUPT", "Completed summary is inconsistent or missing.", 503)
        return ([{"page": p.page, "markdown": p.markdown} for p in parsed.pages], summary)

    async def _commit(self, doc_id: UUID | str, owner_token: str | None,
                      operations: list[tuple[str, tuple, dict]],
                      *, cache_snapshot: str | None = None) -> list[Any]:
        """Fenced atomic update; cache snapshots require active ownership too.

        WATCH protects both the worker's ownership token and the active cache
        pointer. A late worker may finish its own document, but cannot replace
        a result published by the newer cache owner.
        """
        if owner_token is None and cache_snapshot is None:
            pipe = self.redis.pipeline(transaction=True)
            for command, args, kwargs in operations:
                getattr(pipe, command)(*args, **kwargs)
            replies = await pipe.execute(raise_on_error=False)
        else:
            try:
                async with self.redis.pipeline(transaction=True) as pipe:
                    meta_key = self.key(doc_id, "meta")
                    await pipe.watch(meta_key)
                    if owner_token is not None:
                        actual = await pipe.hget(meta_key, "owner_token")
                        if actual != owner_token:
                            raise ProcessingError("JOB_LEASE_LOST", "A different worker owns the job.")
                    final_operations = list(operations)
                    if cache_snapshot is not None:
                        digest = await pipe.hget(meta_key, "cache_key")
                        if digest:
                            key = active_key(digest)
                            await pipe.watch(key)
                            current_owner = await pipe.get(key)
                            if current_owner == str(doc_id):
                                final_operations.append(("set", (result_key(digest), cache_snapshot),
                                                         {"ex": self.cache_ttl}))
                    pipe.multi()
                    for command, args, kwargs in final_operations:
                        getattr(pipe, command)(*args, **kwargs)
                    replies = await pipe.execute(raise_on_error=False)
                    operations = final_operations
            except WatchError as exc:
                raise ProcessingError("JOB_LEASE_LOST", "Job ownership changed during commit.") from exc
        verify_exec(replies, len(operations))
        return replies

    async def refresh_cache_reservation(self, doc_id: UUID | str) -> None:
        """Keep active deduplication alive while a real worker is progressing."""
        meta = await self.get_meta(doc_id)
        if not meta or not meta.get("cache_key"):
            return
        script = """
        -- cache:refresh
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        await self.redis.eval(script, 1, active_key(meta["cache_key"]), str(doc_id), self.cache_ttl)

    @staticmethod
    def parse_page_progress(meta: dict[str, str] | None) -> PageProgress | None:
        """Do not reveal stale or malformed progress after a state transition."""
        if not meta or meta.get('status') != 'processing':
            return None
        try:
            page = int(meta['progress_page'])
            total = int(meta['progress_total'])
            attempt = int(meta['progress_attempt'])
            maximum = int(meta['progress_max_attempts'])
            if not (1 <= page <= total <= 10_000 and 1 <= attempt <= maximum <= 3):
                return None
            return PageProgress(page=page, pages_total=total, attempt=attempt, max_attempts=maximum)
        except (ValueError, TypeError, KeyError):
            return None

    async def set_page_progress(self, doc_id: UUID | str, page: int, pages_total: int,
                                *, owner_token: str | None = None,
                                attempt: int = 1, max_attempts: int = 1) -> None:
        """Fenced Redis-only progress; never write a RustFS manifest per page.

        This is best-effort UI metadata, not a durable count of completed pages.
        The owner token prevents an old worker from overwriting a new owner's state.
        """
        if not (1 <= page <= pages_total <= 10_000 and 1 <= attempt <= max_attempts <= 3):
            raise ValueError('Invalid page progress')
        key = self.key(doc_id, 'meta')
        await self._commit(doc_id, owner_token, [
            ('hset', (key,), {'mapping': {
                'progress_page': str(page), 'progress_total': str(pages_total),
                'progress_attempt': str(attempt), 'progress_max_attempts': str(max_attempts),
            }}),
        ])

    async def mark_processing(self, doc_id: UUID | str, owner_token: str | None = None) -> int:
        key = self.key(doc_id, "meta")
        operations = [
            ("hincrby", (key, "worker_attempts", 1), {}),
            ("hset", (key,), {"mapping": {"status": "processing", "updated_at": now_utc(),
                                     "error_code": "", "error_message": "", "error_diagnostic": "",
                                     "progress_page": "", "progress_total": "",
                                     "progress_attempt": "", "progress_max_attempts": ""}}),
        ]
        replies = await self._commit(doc_id, owner_token, operations)
        return int(replies[0])

    async def mark_summarizing(self, doc_id: UUID | str, owner_token: str | None = None) -> int:
        """Count redeliveries of summary-only retry jobs as well as extraction jobs."""
        key = self.key(doc_id, "meta")
        operations = [
            ("hincrby", (key, "worker_attempts", 1), {}),
            ("hset", (key,), {"mapping": {"status": "summarizing", "updated_at": now_utc(),
                                     "error_code": "", "error_message": "", "error_diagnostic": ""}}),
        ]
        replies = await self._commit(doc_id, owner_token, operations)
        return int(replies[0])

    async def store_extracted(self, doc_id: UUID | str, parsed: ParsedDocument,
                              owner_token: str | None = None) -> None:
        parsed.validate()
        pages = [{"page": p.page, "markdown": p.markdown} for p in parsed.pages]
        operations = [
            ("set", (self.key(doc_id, "pages"), json.dumps(pages, ensure_ascii=False)),
             {"ex": self.result_ttl}),
            ("hset", (self.key(doc_id, "meta"),), {"mapping": {
                "status": "summarizing", "page_count": str(len(pages)),
                "updated_at": now_utc(), "error_code": "", "error_message": "", "error_diagnostic": "",
            }}),
            ("expire", (self.key(doc_id, "meta"), self.result_ttl), {}),
        ]
        await self._commit(doc_id, owner_token, operations)

    async def store_completed(self, doc_id: UUID | str, parsed: ParsedDocument, summary: str,
                              owner_token: str | None = None) -> None:
        parsed.validate()
        if not summary or not summary.strip():
            raise ProcessingError("EMPTY_SUMMARY", "Summarization returned empty content.")
        pages = [{"page": p.page, "markdown": p.markdown} for p in parsed.pages]
        operations = [
            ("set", (self.key(doc_id, "pages"), json.dumps(pages, ensure_ascii=False)),
             {"ex": self.result_ttl}),
            ("set", (self.key(doc_id, "summary"), summary.strip()), {"ex": self.result_ttl}),
            ("hset", (self.key(doc_id, "meta"),), {"mapping": {
                "status": "completed", "page_count": str(len(pages)),
                "updated_at": now_utc(), "error_code": "", "error_message": "", "error_diagnostic": "",
            }}),
            ("expire", (self.key(doc_id, "meta"), self.result_ttl), {}),
        ]
        snapshot = json.dumps({
            "pages": json.dumps(pages, ensure_ascii=False),
            "summary": summary.strip(), "page_count": len(pages), "source_run_id": str(doc_id),
        }, ensure_ascii=False)
        await self._commit(doc_id, owner_token, operations, cache_snapshot=snapshot)

    async def store_partial(self, doc_id: UUID | str, error: ProcessingError,
                            owner_token: str | None = None) -> None:
        await self.read_extracted(doc_id)
        operations = [
            ("delete", (self.key(doc_id, "summary"),), {}),
            ("hset", (self.key(doc_id, "meta"),), {"mapping": {
                "status": "partial", "updated_at": now_utc(),
                "error_code": error.code, "error_message": error.message,
                "error_diagnostic": json.dumps(sanitize_diagnostic(error.diagnostic)) if error.diagnostic else "",
            }}),
            ("expire", (self.key(doc_id, "meta"), self.result_ttl), {}),
            ("expire", (self.key(doc_id, "pages"), self.result_ttl), {}),
        ]
        await self._commit(doc_id, owner_token, operations)

    async def store_failed(self, doc_id: UUID | str, error: ProcessingError,
                           owner_token: str | None = None) -> None:
        operations = [
            ("delete", (self.key(doc_id, "pages"), self.key(doc_id, "summary")), {}),
            ("hset", (self.key(doc_id, "meta"),), {"mapping": {
                "status": "failed", "page_count": "", "updated_at": now_utc(),
                "error_code": error.code, "error_message": error.message,
                "error_diagnostic": json.dumps(sanitize_diagnostic(error.diagnostic)) if error.diagnostic else "",
            }}),
            ("expire", (self.key(doc_id, "meta"), self.result_ttl), {}),
        ]
        await self._commit(doc_id, owner_token, operations)

    def prepare_meta(self, doc_id: UUID, filename: str, parser: str) -> dict[str, str]:
        created = now_utc()
        return {
            "id": str(doc_id), "filename": filename, "parser": parser,
            "status": "queued", "created_at": created, "updated_at": created,
            "page_count": "", "error_code": "", "error_message": "", "error_diagnostic": "", "worker_attempts": "0",
        }
