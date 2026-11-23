"""Phase 4: document-level SHA-256 cache and single-flight uploads; no paid calls."""
from __future__ import annotations
import asyncio
import hashlib
from uuid import UUID, uuid4

import httpx
import pytest

from app.cache import active_key, fingerprint, result_key
from app.config import Settings
from app.document_processor import DocumentProcessor
from app.errors import ProcessingError
from app.main import app
from app.object_store import LocalTestObjectStore
from app.parsers.base import ParsedDocument, ParsedPage
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis


class CountSummaries:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def summarize(self, pages):
        self.calls += 1
        if self.fail:
            raise ProcessingError("SUMMARY_UNAVAILABLE", "Simulated model failure")
        return "One paid summary avoided for duplicates."


def install(redis, settings):
    namespace = f"cache-test:{uuid4()}"
    repo = DocumentRepository(redis, prefix=f"{namespace}:documents", cache_ttl=settings.cache_ttl_seconds)
    queue = DocumentQueue(redis, namespace + ":stream", namespace + ":workers", "worker-a")
    store = LocalTestObjectStore(settings.upload_dir)
    app.state.settings = settings
    app.state.redis = redis
    app.state.repository = repo
    app.state.queue = queue
    app.state.object_store = store
    return repo, queue, store


async def submit(http, sample, parser="pypdf", filename="sample.pdf"):
    response = await http.post("/api/v1/documents", data={"parser": parser}, files={
        "files": (filename, sample.read_bytes(), "application/pdf")
    })
    assert response.status_code == 202, response.text
    return response.json()["documents"][0]


def test_sha256_fingerprint_changes_with_parser_and_model(settings, sample_basic):
    sha = hashlib.sha256(sample_basic.read_bytes()).hexdigest()
    basic = fingerprint(sha, ParserChoice.pypdf, settings)
    assert len(basic) == 64
    assert basic == fingerprint(sha, ParserChoice.pypdf, settings)
    assert basic != fingerprint(sha, ParserChoice.gemini, settings)
    different = settings.model_copy(update={"native_pdf_model": "google/different-model"})
    assert basic != fingerprint(sha, ParserChoice.pypdf, different)
    different_tokens = settings.model_copy(update={"max_ai_output_tokens_per_page": 8192})
    assert basic != fingerprint(sha, ParserChoice.pypdf, different_tokens)
    assert basic != fingerprint(hashlib.sha256(b"different").hexdigest(), ParserChoice.pypdf, settings)


@pytest.mark.asyncio
async def test_same_pdf_two_names_one_job_and_one_summary(settings, sample_basic):
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    summaries = CountSummaries()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=summaries), object_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic, filename="one.pdf")
        second = await submit(http, sample_basic, filename="renamed.pdf")
        assert first["id"] != second["id"]
        assert first["cache"] == "miss" and second["cache"] == "shared"
        assert await redis.xlen(queue.stream) == 1
        assert not await store.exists(UUID(second["id"]))
        waiting = await http.get(f"/api/v1/documents/{second['id']}")
        assert waiting.json()["status"] == "queued"
        await worker.handle_message(*(await queue.read(">"))[0])
        assert summaries.calls == 1
        assert await redis.xlen(queue.stream) == 0
        original = (await http.get(f"/api/v1/documents/{first['id']}" )).json()
        shared = (await http.get(f"/api/v1/documents/{second['id']}" )).json()
        assert original["status"] == shared["status"] == "completed"
        assert shared["filename"] == "renamed.pdf" and shared["cache"] == "shared"
        assert shared["summary"] == original["summary"]
        result_pages = (await http.get(f"/api/v1/documents/{second['id']}/pages")).json()
        assert len(result_pages["pages"]) == 2
        assert result_pages["id"] == second["id"]
        third = await submit(http, sample_basic, filename="other-name.pdf")
        assert third["cache"] == "hit" and third["status"] == "completed"
        assert third["id"] not in {first["id"], second["id"]}
        assert (await http.get(f"/api/v1/documents/{third['id']}")).json()["summary"] == shared["summary"]
        assert await redis.xlen(queue.stream) == 0
        assert summaries.calls == 1
        assert not await store.exists(UUID(third["id"]))


@pytest.mark.asyncio
async def test_simultaneous_identical_uploads_share_one_job(settings, sample_basic):
    redis = FakeRedis()
    _, queue, _ = install(redis, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        results = await asyncio.gather(*(submit(http, sample_basic, filename=f"copy-{i}.pdf") for i in range(5)))
        assert len({r["id"] for r in results}) == 5
        assert sorted(r["cache"] for r in results) == ["miss"] + ["shared"] * 4
        assert await redis.xlen(queue.stream) == 1


@pytest.mark.asyncio
async def test_parser_change_causes_cache_miss(settings, sample_basic):
    redis = FakeRedis()
    _, queue, _ = install(redis, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await submit(http, sample_basic, "pypdf"))["cache"] == "miss"
        assert (await submit(http, sample_basic, "gemini"))["cache"] == "miss"
        assert (await submit(http, sample_basic, "gpt5_mini"))["cache"] == "miss"
        assert await redis.xlen(queue.stream) == 3


@pytest.mark.asyncio
async def test_failed_processing_is_not_cached(settings, sample_scanned):
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo), object_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_scanned, "pypdf")
        await worker.handle_message(*(await queue.read(">"))[0])
        assert (await http.get(f"/api/v1/documents/{first['id']}")).json()["status"] == "failed"
        second = await submit(http, sample_scanned, "pypdf")
        assert second["cache"] == "miss" and second["status"] == "queued"
        assert await redis.xlen(queue.stream) == 1


@pytest.mark.asyncio
async def test_partial_result_is_not_successfully_cached(settings, sample_basic):
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=CountSummaries(fail=True)),
                            object_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic)
        await worker.handle_message(*(await queue.read(">"))[0])
        assert (await http.get(f"/api/v1/documents/{first['id']}")).json()["status"] == "partial"
        second = await submit(http, sample_basic)
        assert second["cache"] == "miss"
        assert await redis.xlen(queue.stream) == 1


@pytest.mark.asyncio
async def test_completed_cache_expiration_triggers_new_job(settings, sample_basic):
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=CountSummaries()),
                            object_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic)
        await worker.handle_message(*(await queue.read(">"))[0])
        third = await submit(http, sample_basic)
        assert third["cache"] == "hit"
        now = redis.clock()
        redis.clock = lambda: now + settings.cache_ttl_seconds + 2
        fourth = await submit(http, sample_basic)
        assert fourth["cache"] == "miss"
        assert await redis.xlen(queue.stream) == 1


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_atomic_simultaneous_registration(real_redis, settings, sample_basic):
    """Actual Redis EVAL must coalesce concurrent requests; no AI calls."""
    redis = real_redis
    settings = settings.model_copy(update={"native_pdf_model": f"test-only-{uuid4()}"})
    repo, queue, _ = install(redis, settings)
    entries = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            entries = await asyncio.gather(*(submit(http, sample_basic, filename=f"n{i}.pdf") for i in range(5)))
        assert len({e["id"] for e in entries}) == 5
        assert sorted(e["cache"] for e in entries) == ["miss"] + ["shared"] * 4
        assert await redis.xlen(queue.stream) == 1
    finally:
        sha = hashlib.sha256(sample_basic.read_bytes()).hexdigest()
        digest = fingerprint(sha, ParserChoice.pypdf, settings)
        await redis.delete(active_key(digest), result_key(digest), queue.stream,
                           *(repo.key(e["id"], "meta") for e in entries))
        # This test only registers jobs, no worker ever owns or removes the PDF.
        for e in entries:
            await app.state.object_store.remove(UUID(e["id"]))


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_cache_hit_after_one_worker_run(real_redis, settings, sample_basic):
    """Real Redis verifies the Lua snapshot AND no second processing job."""
    redis = real_redis
    settings = settings.model_copy(update={"native_pdf_model": f"test-only-{uuid4()}"})
    repo, queue, store = install(redis, settings)
    summaries = CountSummaries()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, summarizer=summaries), object_store=store)
    entries = []
    try:
        await queue.ensure_group()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            first = await submit(http, sample_basic)
            entries.append(first)
            await worker.handle_message(*(await queue.read(">"))[0])
            assert summaries.calls == 1
            second = await submit(http, sample_basic, filename="renamed.pdf")
            entries.append(second)
            assert second["cache"] == "hit" and second["status"] == "completed"
            assert (await http.get(f"/api/v1/documents/{second['id']}")).json()["summary"]
            assert len((await http.get(f"/api/v1/documents/{second['id']}/pages")).json()["pages"]) == 2
            assert await redis.xlen(queue.stream) == 0
            assert summaries.calls == 1
            assert not await store.exists(UUID(second["id"]))
    finally:
        digest = fingerprint(hashlib.sha256(sample_basic.read_bytes()).hexdigest(), ParserChoice.pypdf, settings)
        await redis.delete(active_key(digest), result_key(digest), queue.stream,
                           *(repo.key(e["id"], suffix) for e in entries for suffix in ("meta", "pages", "summary")))
        for e in entries:
            await store.remove(UUID(e["id"]))


@pytest.mark.asyncio
async def test_old_worker_result_cannot_overwrite_new_cache_owner(settings, sample_basic):
    """Ownership fencing is also applied to the cached snapshot."""
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic)
    doc_id = UUID(first["id"])
    digest = fingerprint(hashlib.sha256(sample_basic.read_bytes()).hexdigest(), ParserChoice.pypdf, settings)
    await redis.set(active_key(digest), str(uuid4()), ex=settings.cache_ttl_seconds)
    parsed = ParsedDocument([ParsedPage(1, "All pages")])
    await repo.store_completed(doc_id, parsed, "Old result")
    assert await redis.get(result_key(digest)) is None
    assert (await repo.get_meta(doc_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_duplicate_source_object_cleanup(settings, sample_basic):
    redis = FakeRedis()
    _, queue, store = install(redis, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic)
        second = await submit(http, sample_basic)
        assert first["cache"] == "miss"
        assert second["cache"] == "shared"
        assert await store.exists(UUID(first["id"]))
        assert not await store.exists(UUID(second["id"]))
        assert await redis.xlen(queue.stream) == 1


@pytest.mark.asyncio
async def test_ai_extraction_and_summary_called_once_for_duplicate_gemini_uploads(settings, sample_basic):
    """Mocked AI stage counters prove duplicates never invoke model requests."""
    redis = FakeRedis()
    repo, queue, store = install(redis, settings)
    await queue.ensure_group()
    summary = CountSummaries()
    processor = DocumentProcessor(settings, repo, summarizer=summary)
    extraction_calls = []

    async def mock_gemini_extract(parser, path):
        extraction_calls.append((parser, path.read_bytes()[:5]))
        return ParsedDocument([ParsedPage(1, "# Extracted by Gemini (mock)")])

    processor.extract = mock_gemini_extract
    worker = DocumentWorker(settings, queue, repo, processor, object_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first, second = await asyncio.gather(
            submit(http, sample_basic, "gemini", filename="one.pdf"),
            submit(http, sample_basic, "gemini", filename="same-bytes.pdf"),
        )
        assert sorted([first["cache"], second["cache"]]) == ["miss", "shared"]
        await worker.handle_message(*(await queue.read(">"))[0])
        third = await submit(http, sample_basic, "gemini", filename="new-name.pdf")
        assert third["cache"] == "hit"
        assert len(extraction_calls) == 1 and extraction_calls[0][0] == "gemini"
        assert summary.calls == 1
        assert await redis.xlen(queue.stream) == 0


@pytest.mark.asyncio
async def test_cache_owner_crash_recovery_preserves_single_result(settings, sample_basic):
    """After XAUTOCLAIM, the recovered job is cached without another extraction."""
    redis = FakeRedis()
    repo, queue_a, store = install(redis, settings)
    queue_b = DocumentQueue(redis, queue_a.stream, queue_a.group, "worker-b")
    await queue_a.ensure_group()
    summaries = CountSummaries()
    processor = DocumentProcessor(settings, repo, summarizer=summaries)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        first = await submit(http, sample_basic)
        document_id = UUID(first["id"])
        message_id, fields = (await queue_a.read(">"))[0]
        assert await queue_a.begin(message_id, document_id, "worker-a-token", repo)
        await repo.mark_processing(document_id, owner_token="worker-a-token")
        await repo.store_extracted(document_id, ParsedDocument([ParsedPage(1, "already extracted")]),
                                   owner_token="worker-a-token")
        # Simulate crash: pending message is now idle; worker A no longer heartbeats.
        redis.groups[(queue_a.stream, queue_a.group)]["pel"][message_id]["last_ms"] -= 100000
        claimed = await queue_b.claim_abandoned(1000)
        assert claimed[0][0] == message_id
        async def must_not_extract(*args):
            raise AssertionError("Recovery should reuse already-extracted pages")
        processor.extract = must_not_extract
        worker_b = DocumentWorker(settings, queue_b, repo, processor, object_store=store)
        await worker_b.handle_message(*claimed[0])
        assert summaries.calls == 1
        assert not await store.exists(document_id)
        cached = await submit(http, sample_basic)
        assert cached["cache"] == "hit"
        assert summaries.calls == 1
        assert await redis.xlen(queue_a.stream) == 0
