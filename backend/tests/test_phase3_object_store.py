"""Phase 3: S3 integration, cleanup and failure tests. No paid model calls."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import patch

import httpx
import pytest
from botocore.exceptions import ClientError
from boto3.exceptions import S3UploadFailedError

from app.config import Settings
from app.errors import ProcessingError
from app.main import app
from app.object_store import S3ObjectStore, object_key
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.document_processor import DocumentProcessor
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis


def client_error(code: str, status=404):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "FakeOperation")


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.bucket_exists = False
        self.lifecycle = {}
        self.fail_upload = False
        self.fail_download = False
        self.fail_remove = False
        self.fail_prepare = False

    def head_bucket(self, *, Bucket):
        if not self.bucket_exists:
            raise client_error("404")

    def create_bucket(self, *, Bucket):
        self.bucket_exists = True

    def put_bucket_lifecycle_configuration(self, *, Bucket, LifecycleConfiguration):
        if self.fail_prepare:
            raise client_error("NotImplemented", 501)
        self.lifecycle = LifecycleConfiguration

    def get_bucket_lifecycle_configuration(self, *, Bucket):
        return self.lifecycle

    def upload_file(self, path, bucket, key, ExtraArgs):
        if self.fail_upload:
            raise client_error("ServiceUnavailable", 503)
        self.objects[key] = Path(path).read_bytes()

    def download_file(self, bucket, key, path):
        if self.fail_download:
            raise client_error("ServiceUnavailable", 503)
        if key not in self.objects:
            raise client_error("NoSuchKey")
        Path(path).write_bytes(self.objects[key])

    def delete_object(self, *, Bucket, Key):
        if self.fail_remove:
            raise client_error("ServiceUnavailable", 503)
        self.objects.pop(Key, None)

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise client_error("404")
        return {"ContentLength": len(self.objects[Key])}


@pytest.fixture
def fake_s3(settings):
    client = FakeS3Client()
    with patch("app.object_store.boto3.client", return_value=client):
        store = S3ObjectStore(settings)
    return store, client


def test_s3_object_keys_are_uuid_only():
    uid = uuid4()
    assert object_key(uid) == f"uploads/{uid}.pdf"
    with pytest.raises(ProcessingError, match="INVALID_DOCUMENT_ID"):
        object_key("../../secrets")


@pytest.mark.asyncio
async def test_upload_download_and_remove_roundtrip(fake_s3, sample_basic, tmp_path):
    store, client = fake_s3
    await store.prepare()
    assert client.bucket_exists
    rule, = client.lifecycle["Rules"]
    assert rule["Filter"]["Prefix"] == "uploads/"
    assert rule["Expiration"]["Days"] == 1
    uid = uuid4()
    await store.put(uid, sample_basic)
    assert await store.exists(uid)
    out = tmp_path / "roundtrip.pdf"
    await store.fetch(uid, out)
    assert out.read_bytes() == sample_basic.read_bytes()
    await store.remove(uid)
    assert not await store.exists(uid)
    with pytest.raises(ProcessingError, match="SOURCE_FILE_MISSING"):
        await store.fetch(uid, out)


@pytest.mark.asyncio
async def test_unsupported_s3_lifecycle_is_not_silently_accepted(fake_s3):
    store, client = fake_s3
    client.fail_prepare = True
    with pytest.raises(ProcessingError, match="STORAGE_UNAVAILABLE"):
        await store.prepare()


@pytest.mark.asyncio
async def test_s3_fetch_outage_leaves_redis_job_unacknowledged(fake_s3, settings, sample_basic):
    store, client = fake_s3
    await store.prepare()
    uid = uuid4()
    await store.put(uid, sample_basic)
    client.fail_download = True
    fake = FakeRedis()
    queue = DocumentQueue(fake, "s3:outage", "g", "worker")
    repo = DocumentRepository(fake, "s3:docs")
    await queue.ensure_group()
    doc = StagedFile(uid, "demo.pdf", ParserChoice.pypdf, sample_basic, sample_basic,
                     sample_basic.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo), object_store=store)
    entry = (await queue.read(">"))[0]
    with pytest.raises(ProcessingError, match="STORAGE_UNAVAILABLE"):
        await worker.handle_message(*entry)
    assert (await fake.xpending(queue.stream, queue.group))["pending"] == 1
    assert (await repo.get_meta(uid))["status"] == "processing"
    assert await store.exists(uid)


@pytest.mark.asyncio
async def test_http_upload_object_storage_failure_does_not_enqueue(fake_s3, settings, sample_basic):
    store, client = fake_s3
    await store.prepare()
    client.fail_upload = True
    fake = FakeRedis()
    app.state.settings = settings
    app.state.redis = fake
    app.state.object_store = store
    app.state.repository = DocumentRepository(fake, "s3:http")
    app.state.queue = DocumentQueue(fake, "s3:http:jobs", "group", "consumer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert not fake.streams
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_boto3_transfer_upload_failure_is_sanitized(fake_s3, settings, sample_basic):
    store, client = fake_s3
    await store.prepare()
    def failed_upload(*args, **kwargs):
        raise S3UploadFailedError("Provider credentials or network unavailable")
    client.upload_file = failed_upload
    with pytest.raises(ProcessingError) as failure:
        await store.put(uuid4(), sample_basic)
    assert failure.value.code == "STORAGE_UNAVAILABLE"
    assert "credentials" not in failure.value.message.lower()


@pytest.mark.asyncio
async def test_unknown_enqueue_retains_s3_source(fake_s3, settings, sample_basic):
    store, _ = fake_s3
    await store.prepare()
    fake = FakeRedis()
    fake.fail_exec_after_commit = True
    app.state.settings = settings
    app.state.redis = fake
    app.state.object_store = store
    app.state.repository = DocumentRepository(fake, "s3:uncertain")
    app.state.queue = DocumentQueue(fake, "s3:uncertain:jobs", "group", "consumer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("demo.pdf", sample_basic.read_bytes(), "application/pdf")
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPLOAD_OUTCOME_UNKNOWN"
    uid = UUID(fake.streams["s3:uncertain:jobs"][0][1]["document_id"])
    assert await store.exists(uid)
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.s3_integration
@pytest.mark.asyncio
async def test_real_rustfs_roundtrip_and_lifecycle(sample_basic, tmp_path):
    from app.config import get_settings
    store = S3ObjectStore(get_settings())
    try:
        await store.ready()
    except ProcessingError:
        pytest.skip("RustFS unavailable outside Docker Compose")
    await store.prepare()
    uid = uuid4()
    try:
        await store.put(uid, sample_basic)
        assert await store.exists(uid)
        target = tmp_path / "from-s3.pdf"
        await store.fetch(uid, target)
        assert target.read_bytes() == sample_basic.read_bytes()
    finally:
        await store.remove(uid)
    assert not await store.exists(uid)


@pytest.mark.s3_integration
@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_s3_and_redis_full_worker_no_paid_calls(real_redis, sample_basic):
    from app.config import get_settings
    settings = get_settings()
    store = S3ObjectStore(settings)
    try:
        await store.ready()
    except ProcessingError:
        pytest.skip("RustFS unavailable outside Docker Compose")
    uid = uuid4()
    await store.put(uid, sample_basic)
    repo = DocumentRepository(real_redis, f"phase3:{uid}")
    queue = DocumentQueue(real_redis, f"phase3:jobs:{uid}", f"phase3:group:{uid}", f"phase3:worker:{uid}")
    await queue.ensure_group()
    doc = StagedFile(uid, "demo.pdf", ParserChoice.pypdf, sample_basic, sample_basic,
                     sample_basic.stat().st_size)
    class Summary:
        async def summarize(self, pages):
            return "S3+Redis integration summary"
    try:
        await queue.enqueue_batch([doc], repo)
        worker = DocumentWorker(settings, queue, repo,
                                DocumentProcessor(settings, repo, summarizer=Summary()), object_store=store)
        await worker.handle_message(*(await queue.read(">"))[0])
        assert (await repo.get_meta(uid))["status"] == "completed"
        pages, summary = await repo.read_completed(uid)
        assert len(pages) == 2 and summary == "S3+Redis integration summary"
        assert not await store.exists(uid)
        assert await real_redis.xlen(queue.stream) == 0
    finally:
        await store.remove(uid)
        await real_redis.delete(queue.stream, *(repo.key(uid, suffix)
                                               for suffix in ("meta", "pages", "summary")))
