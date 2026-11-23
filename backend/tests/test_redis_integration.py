"""Redis 7+ integration tests: automatically run inside Compose; skip if no server."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.document_processor import DocumentProcessor
from app.parsers.base import ParsedDocument, ParsedPage
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.object_store import LocalTestObjectStore
from app.worker import DocumentWorker


class DummySummarizer:
    def __init__(self):
        self.calls = 0

    async def summarize(self, pages):
        self.calls += 1
        return "Real Redis integration test summary"


def create_doc(settings, sample_basic):
    uid = uuid4()
    path = settings.upload_dir / f"{uid}.pdf"
    path.write_bytes(sample_basic.read_bytes())
    return StagedFile(uid, "test.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_stream_group_and_ack(real_redis, settings, sample_basic):
    prefix = f"tests:{uuid4()}"
    queue = DocumentQueue(real_redis, prefix + ":stream", prefix + ":group", prefix + ":worker")
    repo = DocumentRepository(real_redis, prefix + ":document")
    docs = [create_doc(settings, sample_basic) for _ in range(2)]
    await queue.ensure_group()
    await queue.ensure_group()  # BUSYGROUP must be idempotent.
    try:
        await queue.enqueue_batch(docs, repo)
        assert await real_redis.xlen(queue.stream) == 2
        pending = (await queue.read(">"))[0]
        assert (await real_redis.xpending(queue.stream, queue.group))["pending"] == 1
        assert (await queue.read("0"))[0][0] == pending[0]
        summary = DummySummarizer()
        worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=summary))
        await worker.handle_message(*pending)
        assert (await real_redis.xpending(queue.stream, queue.group))["pending"] == 0
        assert summary.calls == 1
        pages, result_summary = await repo.read_completed(docs[0].id)
        assert len(pages) == 2
        assert result_summary == "Real Redis integration test summary"
    finally:
        keys = [repo.key(d.id, suffix) for d in docs for suffix in ("meta", "pages", "summary")]
        await real_redis.delete(queue.stream, *keys)
        for d in docs:
            d.final_path.unlink(missing_ok=True)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_runtime_exec_error_does_not_roll_back(real_redis, settings, sample_basic):
    prefix = f"tests:{uuid4()}"
    queue = DocumentQueue(real_redis, prefix + ":stream", prefix + ":group", prefix + ":worker")
    repo = DocumentRepository(real_redis, prefix + ":document")
    await queue.ensure_group()
    doc = create_doc(settings, sample_basic)
    try:
        # WRONGTYPE on HSET occurs during EXEC; subsequent XADD still succeeds.
        await real_redis.set(repo.key(doc.id, "meta"), "wrong type")
        from app.errors import EnqueueOutcomeUnknown
        with pytest.raises(EnqueueOutcomeUnknown):
            await queue.enqueue_batch([doc], repo)
        assert await real_redis.xlen(queue.stream) == 1
        assert doc.final_path.read_bytes() == sample_basic.read_bytes()  # disk source preserved on uncertain commit
    finally:
        await real_redis.delete(queue.stream, repo.key(doc.id, "meta"))
        doc.final_path.unlink(missing_ok=True)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_partial_summary_only_retry(real_redis, settings, sample_basic):
    """Deterministic failure/retry test against the actual Redis Lua retry script."""
    from app.errors import ProcessingError
    prefix = f"phase1:{uuid4()}"
    queue = DocumentQueue(real_redis, prefix + ":stream", prefix + ":group", prefix + ":worker")
    repo = DocumentRepository(real_redis, prefix + ":doc")
    await queue.ensure_group()
    doc = create_doc(settings, sample_basic)

    class FailsOnce:
        calls = 0
        async def summarize(self, pages):
            self.calls += 1
            if self.calls == 1:
                raise ProcessingError("PROVIDER_TRUNCATED", "Simulated truncated summary.")
            return "Summary succeeds on retry without source PDF."

    summary = FailsOnce()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=summary))
    try:
        await queue.enqueue_batch([doc], repo)
        await worker.handle_message(*(await queue.read(">"))[0])
        assert (await repo.get_meta(doc.id))["status"] == "partial"
        assert len((await repo.read_extracted(doc.id)).pages) == 2
        assert not doc.final_path.exists()
        assert await real_redis.xlen(queue.stream) == 0
        assert await real_redis.ttl(repo.key(doc.id, "pages")) > 0
        await queue.enqueue_summary_retry(doc.id, repo)
        with pytest.raises(ProcessingError, match="SUMMARY_RETRY_NOT_ALLOWED"):
            await queue.enqueue_summary_retry(doc.id, repo)
        assert await real_redis.xlen(queue.stream) == 1
        await worker.handle_message(*(await queue.read(">"))[0])
        pages, final = await repo.read_completed(doc.id)
        assert len(pages) == 2 and "without source PDF" in final
        assert summary.calls == 2
        assert await real_redis.xlen(queue.stream) == 0
        assert (await real_redis.xpending(queue.stream, queue.group))["pending"] == 0
    finally:
        await real_redis.delete(queue.stream, *(
            repo.key(doc.id, suffix) for suffix in ("meta", "pages", "summary")))
        doc.final_path.unlink(missing_ok=True)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_http_upload_partial_retry_to_completed(real_redis, settings, sample_basic):
    """Full HTTP-to-stream-to-worker-to-HTTP cycle with real Redis; no paid calls."""
    import httpx
    from app.errors import ProcessingError
    from app.main import app

    # Earlier runs can leave a completed document in the global SHA-256 cache.
    # Make this test's processing fingerprint unique so it *always* exercises
    # the upload -> queue -> partial -> retry path, regardless of cache history.
    import hashlib
    from app.cache import active_key, fingerprint, result_key
    settings = settings.model_copy(update={"native_pdf_model": f"integration-only-{uuid4()}"})
    cache_digest = fingerprint(
        hashlib.sha256(sample_basic.read_bytes()).hexdigest(), ParserChoice.pypdf, settings
    )

    prefix = f"phase1:http:{uuid4()}"
    queue = DocumentQueue(real_redis, prefix + ":stream", prefix + ":group", prefix + ":worker")
    repo = DocumentRepository(real_redis, prefix + ":doc")
    await queue.ensure_group()
    app.state.settings = settings
    app.state.repository = repo
    app.state.queue = queue
    app.state.redis = real_redis
    app.state.object_store = LocalTestObjectStore(settings.upload_dir)

    class FailsOnce:
        calls = 0
        async def summarize(self, pages):
            self.calls += 1
            if self.calls == 1:
                raise ProcessingError("PROVIDER_UNAVAILABLE", "Simulated summary provider outage.")
            return "Successful summary from previously extracted pages."

    summary = FailsOnce()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=summary))
    doc_id = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            uploaded = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
                "files": ("sample.pdf", sample_basic.read_bytes(), "application/pdf")})
            assert uploaded.status_code == 202, uploaded.text
            document = uploaded.json()["documents"][0]
            assert document["cache"] == "miss", "Integration test unexpectedly hit a previous cache entry"
            doc_id = document["id"]
            initial_jobs = await queue.read(">", block_ms=100)
            assert initial_jobs, "Expected exactly one newly queued extraction job"
            await worker.handle_message(*initial_jobs[0])
            result = await client.get(f"/api/v1/documents/{doc_id}")
            assert result.status_code == 200 and result.json()["status"] == "partial"
            assert result.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
            pages = await client.get(f"/api/v1/documents/{doc_id}/pages")
            assert pages.status_code == 200 and len(pages.json()["pages"]) == 2
            assert not (settings.upload_dir / f"{doc_id}.pdf").exists()
            retry = await client.post(f"/api/v1/documents/{doc_id}/retry-summary")
            assert retry.status_code == 202
            assert (await client.post(f"/api/v1/documents/{doc_id}/retry-summary")).status_code == 409
            retry_jobs = await queue.read(">", block_ms=100)
            assert retry_jobs, "Expected summary-only retry job"
            await worker.handle_message(*retry_jobs[0])
            final = await client.get(f"/api/v1/documents/{doc_id}")
            assert final.status_code == 200 and final.json()["status"] == "completed"
            assert "previously extracted pages" in final.json()["summary"]
            assert summary.calls == 2
            assert await real_redis.xlen(queue.stream) == 0
    finally:
        keys = [repo.key(doc_id, suffix) for suffix in ("meta", "pages", "summary")] if doc_id else []
        await real_redis.delete(queue.stream, active_key(cache_digest), result_key(cache_digest), *keys)
        if doc_id:
            (settings.upload_dir / f"{doc_id}.pdf").unlink(missing_ok=True)
