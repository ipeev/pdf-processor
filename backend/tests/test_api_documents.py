import asyncio
from uuid import uuid4

import httpx
import pytest

from app.main import app
from app.object_store import LocalTestObjectStore
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.worker import DocumentWorker
from app.document_processor import DocumentProcessor
from tests.fake_redis import FakeRedis


def install_fake_backend(fake, settings):
    app.state.redis = fake
    app.state.settings = settings
    app.state.repository = DocumentRepository(fake, "api_documents")
    app.state.queue = DocumentQueue(fake, "api:jobs", "api:group", "api:consumer")
    app.state.object_store = LocalTestObjectStore(settings.upload_dir)


def pdf_payload(sample_basic, parser="pypdf"):
    return {"files": ("original.pdf", sample_basic.read_bytes(), "application/pdf")}, {"parser": parser}


@pytest.mark.asyncio
async def test_upload_two_pdfs(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    payload = [("files", ("one.pdf", sample_basic.read_bytes(), "application/pdf")),
               ("files", ("two.pdf", sample_basic.read_bytes(), "application/pdf"))]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files=payload)
        assert response.status_code == 202, response.text
        docs = response.json()["documents"]
        assert len({item["id"] for item in docs}) == 2
        assert [item["status"] for item in docs] == ["queued", "queued"]
        assert len(fake.streams["api:jobs"]) == 1
        assert [item["cache"] for item in docs] == ["miss", "shared"]
        status = await client.get(f"/api/v1/documents/{docs[0]['id']}")
        assert status.status_code == 200 and status.json()["summary"] is None
        assert (await client.get(f"/api/v1/documents/{docs[0]['id']}/pages")).status_code == 409


@pytest.mark.asyncio
async def test_invalid_batch(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files=[
            ("files", ("okay.pdf", sample_basic.read_bytes(), "application/pdf")),
            ("files", ("bad.pdf", b"THIS IS NOT PDF", "application/pdf")),
        ])
        assert resp.status_code == 415
        assert resp.json()["error"]["code"] == "NOT_A_PDF"
        assert "api:jobs" not in fake.streams
        assert list(settings.upload_dir.glob("*.pdf")) == []


@pytest.mark.asyncio
async def test_unavailable_parser(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "unreleased"}, files={
            "files": ("doc.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNAVAILABLE_PARSER"


@pytest.mark.asyncio
async def test_unknown_document(settings):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        uid = uuid4()
        assert (await client.get(f"/api/v1/documents/{uid}")).status_code == 404
        assert (await client.get(f"/api/v1/documents/{uid}/pages")).status_code == 404


@pytest.mark.asyncio
async def test_corrupt_completed_record_is_not_served(settings):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    uid = uuid4()
    repo = app.state.repository
    meta = repo.prepare_meta(uid, "doc.pdf", "pypdf")
    meta.update({"status": "completed", "page_count": "2"})
    await fake.hset(repo.key(uid, "meta"), mapping=meta)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/v1/documents/{uid}/pages")).status_code == 503
        assert (await client.get(f"/api/v1/documents/{uid}")).status_code == 503


@pytest.mark.asyncio
async def test_overlapping_uploads(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def upload():
            return await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
                "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
            })
        first, second, third = await asyncio.gather(upload(), upload(), upload())
        assert all(r.status_code == 202 for r in [first, second, third])
        assert len(fake.streams["api:jobs"]) == 1
        assert sorted(r.json()["documents"][0]["cache"] for r in [first, second, third]) == ["miss", "shared", "shared"]

@pytest.mark.asyncio
async def test_http_completed_result_after_worker(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    await app.state.queue.ensure_group()

    class Summary:
        async def summarize(self, pages):
            return "Summary across two pages"

    worker = DocumentWorker(settings, app.state.queue, app.state.repository,
                            DocumentProcessor(settings, app.state.repository, summarizer=Summary()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        upload = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert upload.status_code == 202
        uid = upload.json()["documents"][0]["id"]
        await worker.handle_message(*(await app.state.queue.read(">"))[0])
        status = await client.get(f"/api/v1/documents/{uid}")
        pages = await client.get(f"/api/v1/documents/{uid}/pages")
        assert status.status_code == 200 and status.json()["status"] == "completed"
        assert status.json()["summary"] == "Summary across two pages"
        assert pages.status_code == 200
        assert [p["page"] for p in pages.json()["pages"]] == [1, 2]


@pytest.mark.asyncio
async def test_http_uncertain_enqueue_preserves_promoted_file(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    fake.fail_exec_after_commit = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPLOAD_OUTCOME_UNKNOWN"
        source_id = fake.streams["api:jobs"][0][1]["document_id"]
        assert (settings.upload_dir / f"{source_id}.pdf").read_bytes() == sample_basic.read_bytes()
        assert len(fake.streams["api:jobs"]) == 1


@pytest.mark.asyncio
async def test_slow_worker_api_responsive(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    await app.state.queue.ensure_group()
    started, release = asyncio.Event(), asyncio.Event()

    class SlowSummary:
        async def summarize(self, pages):
            started.set()
            await release.wait()
            return "slow summary completed"

    worker = DocumentWorker(settings, app.state.queue, app.state.repository,
                            DocumentProcessor(settings, app.state.repository, summarizer=SlowSummary()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        uploaded = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("first.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        uid = uploaded.json()["documents"][0]["id"]
        task = asyncio.create_task(worker.handle_message(*(await app.state.queue.read(">"))[0]))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=1)
            status = await asyncio.wait_for(client.get(f"/api/v1/documents/{uid}"), timeout=1)
            second = await asyncio.wait_for(client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
                "files": ("second.pdf", sample_basic.read_bytes(), "application/pdf")
            }), timeout=1)
            assert health.status_code == 200
            assert status.json()["status"] == "summarizing"
            assert second.status_code == 202
        finally:
            release.set()
            await task

@pytest.mark.asyncio
async def test_upload_redis_down_returns_503_without_promoting_files(settings, sample_basic):
    fake = FakeRedis()
    fake.available = False
    install_fake_backend(fake, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "REDIS_UNAVAILABLE"
        assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_status_trace_metadata_for_all_jobs_including_success_and_cached(settings):
    """The UI needs backend IDs for complete, failed, queued, and cache-reused jobs."""
    import json
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    repo = app.state.repository
    owner_id, reused_id = uuid4(), uuid4()
    upload_owner, upload_reused = uuid4(), uuid4()
    trace_id = "4ed7e7ceeee61c6e1ca34ecf3fdd71a5"
    meta = repo.prepare_meta(owner_id, "owner.pdf", "pypdf")
    meta.update({"trace_id": trace_id, "upload_id": str(upload_owner)})
    await fake.hset(repo.key(owner_id, "meta"), mapping=meta)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        async def get(uid):
            response = await http.get(f"/api/v1/documents/{uid}")
            assert response.status_code == 200, response.text
            return response.json()
        waiting = await get(owner_id)
        assert waiting["status"] == "queued" and waiting["trace_id"] == trace_id
        assert waiting["upload_id"] == str(upload_owner)
        for state in ("processing", "summarizing"):
            await fake.hset(repo.key(owner_id, "meta"), mapping={"status": state})
            current = await get(owner_id)
            assert current["status"] == state and current["trace_id"] == trace_id
        await fake.hset(repo.key(owner_id, "meta"), mapping={"status": "completed", "page_count": "1"})
        await fake.set(repo.key(owner_id, "pages"), json.dumps([{"page": 1, "markdown": "# OK"}]))
        await fake.set(repo.key(owner_id, "summary"), "Everything completed")
        completed = await get(owner_id)
        assert completed["status"] == "completed" and completed["trace_id"] == trace_id
        reuse_meta = repo.prepare_meta(reused_id, "reused.pdf", "pypdf")
        reuse_meta.update({"cache_source_id": str(owner_id), "cache": "hit", "upload_id": str(upload_reused)})
        await fake.hset(repo.key(reused_id, "meta"), mapping=reuse_meta)
        reused = await get(reused_id)
        assert reused["status"] == "completed" and reused["trace_id"] == trace_id
        assert reused["upload_id"] == str(upload_reused)
        assert reused["id"] == str(reused_id)
        await fake.hset(repo.key(owner_id, "meta"), mapping={
            "status": "failed", "error_code": "PROVIDER_UNAVAILABLE", "error_message": "Timed out",
            "error_diagnostic": json.dumps({"phase": "image_ocr", "page": 6}),
        })
        failed = await get(owner_id)
        assert failed["trace_id"] == trace_id
        assert failed["error"]["diagnostic"]["page"] == 6
        await fake.hset(repo.key(owner_id, "meta"), mapping={"status": "partial", "page_count": "1"})
        partial = await get(owner_id)
        assert partial["trace_id"] == trace_id
        assert partial["upload_id"] == str(upload_owner)
