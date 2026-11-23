"""Object storage for production PDFs; volatile Redis; explicit cleanup."""
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.document_processor import DocumentProcessor
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "docker-compose.yml").is_file())


def test_compose_s3_disk_volume_and_volatile_redis():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    redis = services["redis"]
    command = redis["command"]
    for flag, value in {
        "--appendonly": "no",
        "--save": "",
        "--maxmemory": "1536mb",
        "--maxmemory-policy": "noeviction",
    }.items():
        assert command.count(flag) == 1
        assert command[command.index(flag) + 1] == value
    assert not redis.get("volumes")
    assert any(mount.split(":", 1)[0] == "/data" for mount in redis["tmpfs"])

    assert services["rustfs"]["image"] == "rustfs/rustfs:1.0.3"
    for service in ("rustfs", "rustfs-permissions"):
        assert "rustfs_data:/data" in services[service]["volumes"]
    assert "rustfs_data" in compose["volumes"]
    assert "opensearch_data" in compose["volumes"]
    assert "archive_coordination" in compose["volumes"]
    for service in ("api", "worker", "rustfs-permissions"):
        assert "archive_coordination:/coordination" in services[service]["volumes"]
    assert "opensearch_data:/usr/share/opensearch/data" in services["opensearch"]["volumes"]
    assert "redis_data" not in compose["volumes"]
    assert "uploads" not in compose["volumes"]
    assert all(
        "uploads:/data/uploads" not in service.get("volumes", [])
        for service in services.values()
    )


@pytest.mark.asyncio
async def test_success_deletes_disk_pdf_but_expires_redis_result(settings, sample_basic):
    fake = FakeRedis()
    now = [1000.0]
    fake.clock = lambda: now[0]
    repo = DocumentRepository(fake, 'disk', result_ttl=90)
    queue = DocumentQueue(fake, 'disk:jobs', 'disk:group', 'worker')
    await queue.ensure_group()
    uid = uuid4()
    pdf = settings.upload_dir / f'{uid}.pdf'
    pdf.write_bytes(sample_basic.read_bytes())
    await queue.enqueue_batch([StagedFile(uid, 'doc.pdf', ParserChoice.pypdf, pdf, pdf, pdf.stat().st_size)], repo)
    assert pdf.exists()
    assert await fake.xlen(queue.stream) == 1
    assert not any(':source' in k for k in fake.keys)

    class Summary:
        async def summarize(self, pages):
            return 'A legitimate PDF summary'

    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=Summary()))
    await worker.handle_message(*(await queue.read('>'))[0])
    assert not pdf.exists()
    assert await fake.xlen(queue.stream) == 0
    assert (await repo.get_meta(uid))['status'] == 'completed'
    for suffix in ('meta', 'pages', 'summary'):
        assert await fake.ttl(repo.key(uid, suffix)) > 0
    now[0] += 91
    assert await repo.get_meta(uid) is None
    assert await fake.get(repo.key(uid, 'pages')) is None


@pytest.mark.asyncio
async def test_failure_deletes_disk_pdf(settings, sample_scanned):
    fake = FakeRedis()
    repo = DocumentRepository(fake, 'failed', result_ttl=100)
    queue = DocumentQueue(fake, 'failed:jobs', 'failed:group', 'worker')
    await queue.ensure_group()
    uid = uuid4()
    pdf = settings.upload_dir / f'{uid}.pdf'
    pdf.write_bytes(sample_scanned.read_bytes())
    await queue.enqueue_batch([StagedFile(uid, 'scan.pdf', ParserChoice.pypdf, pdf, pdf, pdf.stat().st_size)], repo)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read('>'))[0])
    assert (await repo.get_meta(uid))['error_code'] == 'NO_EXTRACTABLE_TEXT'
    assert await fake.xlen(queue.stream) == 0
    assert not pdf.exists()
    assert await fake.ttl(repo.key(uid, 'meta')) > 0


@pytest.mark.asyncio
async def test_unfinished_source_stays_on_disk_while_job_is_pending(settings, sample_basic):
    fake = FakeRedis()
    repo = DocumentRepository(fake, 'queued')
    queue = DocumentQueue(fake, 'queued:jobs', 'queued:group', 'worker')
    await queue.ensure_group()
    uid = uuid4()
    pdf = settings.upload_dir / f'{uid}.pdf'
    pdf.write_bytes(sample_basic.read_bytes())
    await queue.enqueue_batch([StagedFile(uid, 'queued.pdf', ParserChoice.pypdf, pdf, pdf, pdf.stat().st_size)], repo)
    assert pdf.is_file()
    assert (await repo.get_meta(uid))['status'] == 'queued'
    assert await fake.xlen(queue.stream) == 1


def test_new_redis_instance_has_no_previous_keys_or_jobs():
    old = FakeRedis()
    old.keys['example'] = 'not retained'
    new = FakeRedis()
    assert not new.keys and not new.streams
