"""Phase 2: two workers, fencing, takeover and heartbeat. No paid model calls."""
import asyncio
from uuid import uuid4

import pytest

from app.document_processor import DocumentProcessor
from app.errors import ProcessingError
from app.parsers.base import ParsedDocument, ParsedPage
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis


class StaticSummary:
    def __init__(self, result="Valid summary"):
        self.calls = 0
        self.result = result

    async def summarize(self, pages):
        self.calls += 1
        return self.result


def staged(settings, sample):
    doc_id = uuid4()
    path = settings.upload_dir / f"{doc_id}.pdf"
    path.write_bytes(sample.read_bytes())
    return StagedFile(doc_id, "test.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)


@pytest.mark.asyncio
async def test_two_consumers_process_different_jobs(settings, sample_basic):
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    one = DocumentQueue(redis, "multi:jobs", "group", "consumer-a")
    two = DocumentQueue(redis, "multi:jobs", "group", "consumer-b")
    await one.ensure_group()
    docs = [staged(settings, sample_basic) for _ in range(2)]
    await one.enqueue_batch(docs, repo)
    first, second = (await one.read(">"))[0], (await two.read(">"))[0]
    assert first[0] != second[0]
    workers = [DocumentWorker(settings, q, repo, DocumentProcessor(settings, repo, summarizer=StaticSummary()))
               for q in (one, two)]
    await asyncio.gather(workers[0].handle_message(*first), workers[1].handle_message(*second))
    assert [ (await repo.get_meta(doc.id))["status"] for doc in docs ] == ["completed", "completed"]
    assert await redis.xlen(one.stream) == 0
    assert all(not doc.final_path.exists() for doc in docs)


@pytest.mark.asyncio
async def test_takeover_reuses_extracted_pages_and_blocks_stale_writes(settings, sample_basic):
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    one = DocumentQueue(redis, "takeover:jobs", "group", "consumer-a")
    two = DocumentQueue(redis, "takeover:jobs", "group", "consumer-b")
    await one.ensure_group()
    doc = staged(settings, sample_basic)
    await one.enqueue_batch([doc], repo)
    message, fields = (await one.read(">"))[0]
    assert await one.begin(message, doc.id, "old-token", repo)
    await repo.mark_processing(doc.id, owner_token="old-token")
    parsed = await DocumentProcessor(settings, repo).extract("pypdf", doc.final_path)
    await repo.store_extracted(doc.id, parsed, owner_token="old-token")
    # Simulate a worker disappearing: no more heartbeats.
    redis.groups[(one.stream, one.group)]["pel"][message]["last_ms"] -= 100000
    claimed = await two.claim_abandoned(1000)
    assert claimed[0][0] == message
    assert await two.begin(message, doc.id, "new-token", repo)
    with pytest.raises(ProcessingError, match="JOB_LEASE_LOST"):
        await repo.store_completed(doc.id, parsed, "stale result", owner_token="old-token")
    assert not await one.ack_owned(message, doc.id, "old-token", repo)
    worker = DocumentWorker(settings, two, repo,
                            DocumentProcessor(settings, repo, summarizer=StaticSummary("Recovered summary")))
    async def unexpected_extract(*_):
        raise AssertionError("Worker replay must reuse saved extraction")
    worker.processor.extract = unexpected_extract
    await worker.handle_message(message, fields)
    assert (await repo.get_meta(doc.id))["status"] == "completed"
    _, summary = await repo.read_completed(doc.id)
    assert summary == "Recovered summary"
    assert not doc.final_path.exists()
    assert (await redis.xpending(two.stream, two.group))["pending"] == 0


@pytest.mark.asyncio
async def test_heartbeat_prevents_takeover(settings, sample_basic):
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    one = DocumentQueue(redis, "beat:jobs", "group", "consumer-a")
    two = DocumentQueue(redis, "beat:jobs", "group", "consumer-b")
    await one.ensure_group()
    doc = staged(settings, sample_basic)
    await one.enqueue_batch([doc], repo)
    msg, _ = (await one.read(">"))[0]
    assert await one.begin(msg, doc.id, "token", repo)
    redis.groups[(one.stream, one.group)]["pel"][msg]["last_ms"] -= 100000
    assert await one.heartbeat(msg)
    assert await two.claim_abandoned(1000) == []
    assert await one.ack_owned(msg, doc.id, "token", repo)
    doc.final_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stale_worker_cannot_mark_failure(settings, sample_basic):
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    one = DocumentQueue(redis, "fence:jobs", "group", "consumer-a")
    two = DocumentQueue(redis, "fence:jobs", "group", "consumer-b")
    await one.ensure_group()
    doc = staged(settings, sample_basic)
    await one.enqueue_batch([doc], repo)
    msg, _ = (await one.read(">"))[0]
    assert await one.begin(msg, doc.id, "token-old", repo)
    redis.groups[(one.stream, one.group)]["pel"][msg]["last_ms"] -= 100000
    assert await two.claim_abandoned(1000)
    assert await two.begin(msg, doc.id, "token-new", repo)
    with pytest.raises(ProcessingError, match="JOB_LEASE_LOST"):
        await repo.store_failed(doc.id, ProcessingError("ERROR", "Stale failure"), owner_token="token-old")
    assert (await repo.get_meta(doc.id))["status"] == "queued"
    doc.final_path.unlink(missing_ok=True)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_two_worker_crash_and_takeover(real_redis, settings, sample_basic):
    """Real Redis Streams: cancel interrupted worker, let another claim and finish."""
    prefix = f"phase2:{uuid4()}"
    repo = DocumentRepository(real_redis, prefix + ":doc")
    one = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-a")
    two = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-b")
    await one.ensure_group()
    doc = staged(settings, sample_basic)
    started = asyncio.Event()
    stalled = asyncio.Event()

    class InterruptedSummary:
        async def summarize(self, pages):
            started.set()
            await stalled.wait()
            return "This response must never be committed."

    first_worker = DocumentWorker(settings, one, repo,
                                  DocumentProcessor(settings, repo, summarizer=InterruptedSummary()))
    survivor = DocumentWorker(settings, two, repo,
                              DocumentProcessor(settings, repo, summarizer=StaticSummary("Recovered result")))
    task = None
    try:
        await one.enqueue_batch([doc], repo)
        message, fields = (await one.read(">"))[0]
        task = asyncio.create_task(first_worker.handle_message(message, fields))
        await asyncio.wait_for(started.wait(), timeout=10)
        assert (await repo.get_meta(doc.id))["status"] == "summarizing"
        task.cancel()  # Deterministic equivalent of a terminated worker process.
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.15)
        claimed = await two.claim_abandoned(100)
        assert claimed and claimed[0][0] == message
        async def forbidden(*_):
            raise AssertionError("Recovered job repeated paid extraction")
        survivor.processor.extract = forbidden
        await survivor.handle_message(*claimed[0])
        _, summary = await repo.read_completed(doc.id)
        assert summary == "Recovered result"
        assert (await real_redis.xpending(one.stream, one.group))["pending"] == 0
        assert await real_redis.xlen(one.stream) == 0
        assert not doc.final_path.exists()
    finally:
        if task and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await real_redis.delete(one.stream, *(repo.key(doc.id, suffix)
                                              for suffix in ("meta", "pages", "summary")))
        doc.final_path.unlink(missing_ok=True)


@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_heartbeat_and_stale_fencing(real_redis, settings, sample_basic):
    prefix = f"phase2:fence:{uuid4()}"
    repo = DocumentRepository(real_redis, prefix + ":doc")
    one = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-a")
    two = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-b")
    doc = staged(settings, sample_basic)
    await one.ensure_group()
    try:
        await one.enqueue_batch([doc], repo)
        msg, _ = (await one.read(">"))[0]
        assert await one.begin(msg, doc.id, "old", repo)
        await asyncio.sleep(.12)
        assert await one.heartbeat(msg)
        assert await two.claim_abandoned(100) == []
        await asyncio.sleep(.12)
        assert (await two.claim_abandoned(100))[0][0] == msg
        assert await two.begin(msg, doc.id, "new", repo)
        parsed = ParsedDocument([ParsedPage(1, "Original text")])
        with pytest.raises(ProcessingError, match="JOB_LEASE_LOST"):
            await repo.store_completed(doc.id, parsed, "Stale summary", owner_token="old")
        assert not await one.ack_owned(msg, doc.id, "old", repo)
        await repo.store_completed(doc.id, parsed, "New summary", owner_token="new")
        assert await two.ack_owned(msg, doc.id, "new", repo)
        _, summary = await repo.read_completed(doc.id)
        assert summary == "New summary"
    finally:
        await real_redis.delete(one.stream, *(repo.key(doc.id, suffix)
                                              for suffix in ("meta", "pages", "summary")))
        doc.final_path.unlink(missing_ok=True)

@pytest.mark.redis_integration
@pytest.mark.asyncio
async def test_real_redis_long_request_is_not_stolen(real_redis, settings, sample_basic):
    """A slow but live provider call is protected by the worker's heartbeat task."""
    prefix = f"phase2:heartbeat:{uuid4()}"
    repo = DocumentRepository(real_redis, prefix + ":doc")
    one = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-a")
    two = DocumentQueue(real_redis, prefix + ":jobs", prefix + ":group", "consumer-b")
    await one.ensure_group()
    doc = staged(settings, sample_basic)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider:
        async def summarize(self, pages):
            started.set()
            await release.wait()
            return "Slow but valid summary"

    fast_heartbeat_settings = settings.model_copy(update={"worker_heartbeat_seconds": 1})
    worker = DocumentWorker(fast_heartbeat_settings, one, repo,
                            DocumentProcessor(settings, repo, summarizer=SlowProvider()))
    task = None
    try:
        await one.enqueue_batch([doc], repo)
        message = (await one.read(">"))[0]
        task = asyncio.create_task(worker.handle_message(*message))
        await asyncio.wait_for(started.wait(), timeout=10)
        # After >3 seconds without a heartbeat, this message would be eligible
        # for 1.5-second XAUTOCLAIM. The live worker should refresh every second.
        await asyncio.sleep(3.2)
        assert await two.claim_abandoned(1500) == []
        release.set()
        await asyncio.wait_for(task, timeout=10)
        _, summary = await repo.read_completed(doc.id)
        assert summary == "Slow but valid summary"
        assert (await real_redis.xpending(one.stream, one.group))["pending"] == 0
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await real_redis.delete(one.stream, *(repo.key(doc.id, suffix)
                                              for suffix in ("meta", "pages", "summary")))
        doc.final_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_two_workers_cannot_both_claim_the_same_message(settings, sample_basic):
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    first = DocumentQueue(redis, "exclusive:jobs", "group", "consumer-a")
    second = DocumentQueue(redis, "exclusive:jobs", "group", "consumer-b")
    third = DocumentQueue(redis, "exclusive:jobs", "group", "consumer-c")
    await first.ensure_group()
    doc = staged(settings, sample_basic)
    await first.enqueue_batch([doc], repo)
    msg, _ = (await first.read(">"))[0]
    redis.groups[(first.stream, first.group)]["pel"][msg]["last_ms"] -= 100000
    left, right = await asyncio.gather(second.claim_abandoned(1000),
                                       third.claim_abandoned(1000))
    assert bool(left) != bool(right)
    doc.final_path.unlink(missing_ok=True)


def test_phase2_worker_configuration_and_compose_defaults():
    from pathlib import Path
    from pydantic import ValidationError
    from app.config import Settings
    assert Settings(_env_file=None).worker_heartbeat_seconds == 5
    assert Settings(_env_file=None).worker_claim_idle_seconds == 45
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_heartbeat_seconds=10, worker_claim_idle_seconds=20)
    root = next(p for p in Path(__file__).resolve().parents
                if (p / "docker-compose.yml").exists())
    compose = (root / "docker-compose.yml").read_text()
    # Logging configuration is now inserted before the scale key; check the
    # worker service block rather than relying on fixed YAML key order.
    worker_block = compose.split("\n  worker:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert "    scale: 2" in worker_block
    assert "- rustfs_data:/data" in compose
    assert "- uploads:/data/uploads" not in compose
    assert "redis_data:" not in compose
