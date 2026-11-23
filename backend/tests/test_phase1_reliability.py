"""Regression tests for the issues raised during code review."""
import json
from uuid import uuid4

import httpx
import pytest

from app.document_processor import DocumentProcessor
from app.errors import ProcessingError
from app.main import app
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis


class FlakySummary:
    def __init__(self):
        self.calls = 0

    async def summarize(self, pages):
        self.calls += 1
        if self.calls == 1:
            raise ProcessingError("PROVIDER_TRUNCATED", "Model output hit the configured token limit.")
        return "## Summary\n\nSuccessfully summarized without new extraction."


async def start_partial(settings, sample_basic):
    fake = FakeRedis()
    repo = DocumentRepository(fake, "phase1")
    queue = DocumentQueue(fake, "phase1:jobs", "phase1:group", "worker-1")
    await queue.ensure_group()
    doc_id = uuid4()
    path = settings.upload_dir / f"{doc_id}.pdf"
    path.write_bytes(sample_basic.read_bytes())
    doc = StagedFile(doc_id, "source.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    summarizer = FlakySummary()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=summarizer))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc_id))["status"] == "partial"
    return fake, repo, queue, worker, doc, summarizer


@pytest.mark.asyncio
async def test_summary_failure_keeps_pages_and_cleans_pdf(settings, sample_basic):
    fake, repo, queue, worker, doc, summarizer = await start_partial(settings, sample_basic)
    assert await fake.get(repo.key(doc.id, "summary")) is None
    assert len((await repo.read_extracted(doc.id)).pages) == 2
    assert not doc.final_path.exists()
    assert await fake.xlen(queue.stream) == 0
    assert await fake.ttl(repo.key(doc.id, "pages")) > 0
    assert await fake.ttl(repo.key(doc.id, "meta")) > 0


@pytest.mark.asyncio
async def test_summary_only_retry_never_reruns_extraction(settings, sample_basic):
    fake, repo, queue, worker, doc, summary = await start_partial(settings, sample_basic)
    # Prove the source is gone and will not be needed for summary retry.
    assert not doc.final_path.exists()
    async def forbidden_extract(*_):
        raise AssertionError("summary retry attempted paid extraction")
    worker.processor.extract = forbidden_extract
    await queue.enqueue_summary_retry(doc.id, repo)
    assert (await repo.get_meta(doc.id))["status"] == "summarizing"
    with pytest.raises(ProcessingError, match="SUMMARY_RETRY_NOT_ALLOWED"):
        await queue.enqueue_summary_retry(doc.id, repo)
    assert await fake.xlen(queue.stream) == 1
    await worker.handle_message(*(await queue.read(">"))[0])
    pages, final_summary = await repo.read_completed(doc.id)
    assert len(pages) == 2
    assert "without new extraction" in final_summary
    assert summary.calls == 2
    assert await fake.xlen(queue.stream) == 0


@pytest.mark.asyncio
async def test_http_partial_results_and_retry_contract(settings, sample_basic):
    fake, repo, queue, worker, doc, _ = await start_partial(settings, sample_basic)
    app.state.settings = settings
    app.state.redis = fake
    app.state.repository = repo
    app.state.queue = queue
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/v1/documents/{doc.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"
        assert resp.json()["error"]["code"] == "PROVIDER_TRUNCATED"
        assert resp.json()["summary"] is None
        pages = await client.get(f"/api/v1/documents/{doc.id}/pages")
        assert pages.status_code == 200 and len(pages.json()["pages"]) == 2
        retry = await client.post(f"/api/v1/documents/{doc.id}/retry-summary")
        assert retry.status_code == 202 and retry.json()["status"] == "summarizing"
        duplicate = await client.post(f"/api/v1/documents/{doc.id}/retry-summary")
        assert duplicate.status_code == 409
    await worker.handle_message(*(await queue.read(">"))[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        final = await client.get(f"/api/v1/documents/{doc.id}")
        assert final.status_code == 200 and final.json()["status"] == "completed"
        assert final.json()["summary"]
        assert (await client.post(f"/api/v1/documents/{doc.id}/retry-summary")).status_code == 409


@pytest.mark.asyncio
async def test_summary_failure_again_stays_partial(settings, sample_basic):
    fake, repo, queue, worker, doc, summary = await start_partial(settings, sample_basic)
    async def always_fails(pages):
        raise ProcessingError("PROVIDER_UNAVAILABLE", "OpenRouter unavailable.")
    worker.processor.summarizer.summarize = always_fails
    await queue.enqueue_summary_retry(doc.id, repo)
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc.id))["status"] == "partial"
    assert (await repo.get_meta(doc.id))["error_code"] == "PROVIDER_UNAVAILABLE"
    assert len((await repo.read_extracted(doc.id)).pages) == 2
    assert await fake.xlen(queue.stream) == 0


@pytest.mark.asyncio
async def test_extraction_failure_still_has_no_pages(settings, sample_scanned):
    fake = FakeRedis()
    repo = DocumentRepository(fake)
    queue = DocumentQueue(fake, "tests:extraction", "group", "worker")
    await queue.ensure_group()
    doc_id = uuid4()
    path = settings.upload_dir / f"{doc_id}.pdf"
    path.write_bytes(sample_scanned.read_bytes())
    doc = StagedFile(doc_id, "scan.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc_id))["status"] == "failed"
    assert await fake.get(repo.key(doc_id, "pages")) is None


@pytest.mark.asyncio
async def test_replay_extracted_pages_without_reextracting(settings, sample_basic):
    fake = FakeRedis()
    repo = DocumentRepository(fake)
    queue = DocumentQueue(fake, "tests:replay", "group", "worker")
    await queue.ensure_group()
    doc_id = uuid4()
    path = settings.upload_dir / f"{doc_id}.pdf"
    path.write_bytes(sample_basic.read_bytes())
    doc = StagedFile(doc_id, "replay.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    parsed = await DocumentProcessor(settings, repo).extract("pypdf", path)
    await repo.store_extracted(doc_id, parsed)
    path.unlink()  # simulate source no longer available after completed extraction
    class Summary:
        async def summarize(self, pages):
            return "Valid result from saved pages"
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=Summary()))
    async def forbidden_extract(*_):
        raise AssertionError("replay repeated extraction")
    worker.processor.extract = forbidden_extract
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_corrupt_saved_extraction_fails_closed(redis_fake):
    repo = DocumentRepository(redis_fake)
    doc_id = uuid4()
    meta = repo.prepare_meta(doc_id, "file.pdf", "pypdf")
    meta.update({"status": "partial", "page_count": "2"})
    await redis_fake.hset(repo.key(doc_id, "meta"), mapping=meta)
    await redis_fake.set(repo.key(doc_id, "pages"), json.dumps([{"page": 2, "markdown": "oops"}]))
    with pytest.raises(ProcessingError, match="RESULT_CORRUPT"):
        await repo.read_extracted(doc_id)


@pytest.mark.asyncio
async def test_summary_retry_missing_or_completed_is_rejected(redis_fake):
    repo = DocumentRepository(redis_fake)
    queue = DocumentQueue(redis_fake, "retry:jobs", "retry:group", "worker")
    await queue.ensure_group()
    unknown = uuid4()
    with pytest.raises(ProcessingError, match="DOCUMENT_NOT_FOUND"):
        await queue.enqueue_summary_retry(unknown, repo)
    await redis_fake.hset(repo.key(unknown, "meta"), mapping=repo.prepare_meta(unknown, "test.pdf", "pypdf"))
    with pytest.raises(ProcessingError, match="SUMMARY_RETRY_NOT_ALLOWED"):
        await queue.enqueue_summary_retry(unknown, repo)
    await redis_fake.hset(repo.key(unknown, "meta"), mapping={"status": "partial", "page_count": "2"})
    with pytest.raises(ProcessingError, match="RESULT_NOT_READY"):
        await queue.enqueue_summary_retry(unknown, repo)
    assert await redis_fake.xlen(queue.stream) == 0
