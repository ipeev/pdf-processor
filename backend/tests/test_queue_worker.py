from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.document_processor import DocumentProcessor
from app.errors import ProcessingError, EnqueueOutcomeUnknown
from app.parsers.base import ParsedDocument, ParsedPage
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.worker import DocumentWorker


class FakeSummary:
    def __init__(self):
        self.calls = 0

    async def summarize(self, pages):
        self.calls += 1
        return "Summary of all PDF pages"


@pytest.fixture
async def queue_store(redis_fake):
    repo = DocumentRepository(redis_fake, "test_documents")
    queue = DocumentQueue(redis_fake, "test:jobs", "test-group", "test-consumer")
    await queue.ensure_group()
    return queue, repo


def fake_doc(settings, sample_basic):
    uid = uuid4()
    path = settings.upload_dir / f"{uid}.pdf"
    path.write_bytes(sample_basic.read_bytes())
    return StagedFile(uid, "document.pdf", ParserChoice.pypdf, path, path, path.stat().st_size)


@pytest.mark.asyncio
async def test_group_pending_ack(queue_store, settings, sample_basic, redis_fake):
    queue, repo = queue_store
    await queue.ensure_group()
    docs = [fake_doc(settings, sample_basic) for _ in range(2)]
    await queue.enqueue_batch(docs, repo)
    assert len(redis_fake.streams[queue.stream]) == 2
    entries = await queue.read(">")
    assert entries[0][1]["document_id"] == str(docs[0].id)
    assert (await redis_fake.xpending(queue.stream, queue.group))["pending"] == 1
    assert await queue.ack(entries[0][0]) == 1
    assert (await redis_fake.xpending(queue.stream, queue.group))["pending"] == 0


@pytest.mark.asyncio
async def test_processor_lifecycle(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    summary = FakeSummary()
    processor = DocumentProcessor(settings, repo, summarizer=summary)
    worker = DocumentWorker(settings, queue, repo, processor)
    entries = await queue.read(">")
    await worker.handle_message(*entries[0])
    meta = await repo.get_meta(doc.id)
    assert meta["status"] == "completed"
    assert summary.calls == 1
    pages, text = await repo.read_completed(doc.id)
    assert len(pages) == 2 and text.startswith("Summary")
    assert not (await queue.read("0"))


@pytest.mark.asyncio
async def test_replay_already_completed_never_summarizes_again(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    summary = FakeSummary()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=summary))
    entry = (await queue.read(">"))[0]
    await repo.store_completed(doc.id, ParsedDocument([ParsedPage(1, "one")]), "already stored")
    await worker.handle_message(*entry)
    assert summary.calls == 0
    assert (await queue.read("0")) == []


@pytest.mark.asyncio
async def test_crash_before_ack_replay(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    entry = (await queue.read(">"))[0]
    assert (await queue.read("0"))[0][0] == entry[0]
    summary = FakeSummary()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=summary))
    await worker.handle_message(*entry)
    assert (await repo.get_meta(doc.id))["status"] == "completed"
    assert (await queue.read("0")) == []


@pytest.mark.asyncio
async def test_exec_reply_lost_preserves_source(queue_store, settings, sample_basic, redis_fake):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    redis_fake.fail_exec_after_commit = True
    with pytest.raises(EnqueueOutcomeUnknown):
        await queue.enqueue_batch([doc], repo)
    assert doc.final_path.read_bytes() == sample_basic.read_bytes()
    assert (await repo.get_meta(doc.id))["status"] == "queued"
    assert redis_fake.streams[queue.stream]


@pytest.mark.asyncio
async def test_no_fabricated_summary_in_runtime(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read(">"))[0])
    meta = await repo.get_meta(doc.id)
    assert meta["status"] == "partial"
    assert meta["error_code"] == "SUMMARIZER_NOT_CONFIGURED"
    pages = await repo.read_extracted(doc.id)
    assert len(pages.pages) == 2


@pytest.mark.asyncio
async def test_missing_source_fails(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    doc.final_path.unlink()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc.id))["error_code"] == "SOURCE_FILE_MISSING"


@pytest.mark.asyncio
async def test_poison_attempt_limit(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    await repo.mark_processing(doc.id)
    await repo.mark_processing(doc.id)
    await repo.mark_processing(doc.id)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc.id))["error_code"] == "WORKER_RETRIES_EXCEEDED"


@pytest.mark.asyncio
async def test_failed_job_acknowledged_after_persistence(queue_store, settings, sample_basic):
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    await repo.store_failed(doc.id, ProcessingError("INVALID_PDF", "PDF corrupted."))
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert not await queue.read("0")

@pytest.mark.asyncio
async def test_commit_before_ack_recovery_without_duplicate_summary(queue_store, settings, sample_basic):
    from redis.exceptions import ConnectionError as RedisConnectionError
    queue, repo = queue_store
    doc = fake_doc(settings, sample_basic)
    await queue.enqueue_batch([doc], repo)
    summary = FakeSummary()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, summarizer=summary))
    entry = (await queue.read(">"))[0]
    original_ack = queue.ack_owned
    async def interrupted_ack(*args, **kwargs):
        raise RedisConnectionError("simulated connection loss after complete commit")
    queue.ack_owned = interrupted_ack
    with pytest.raises(RedisConnectionError):
        await worker.handle_message(*entry)
    assert (await repo.get_meta(doc.id))["status"] == "completed"
    assert summary.calls == 1
    assert doc.final_path.exists()  # preserve file when ACK was interrupted
    queue.ack_owned = original_ack
    await worker.handle_message(*(await queue.read("0"))[0])
    assert summary.calls == 1
    assert (await queue.read("0")) == []
    assert not doc.final_path.exists()  # clean after successful replay/ACK


def test_worker_shutdown_flag(settings):
    # Signal handlers call this method; the worker must leave its idle poll loop.
    # The request does not cancel a partially completed model call.
    # The fixture is async, so verify the setter without connecting to Redis.
    from unittest.mock import Mock
    worker = DocumentWorker(settings, Mock(), Mock(), Mock())
    assert not worker.stopping
    worker.request_shutdown()
    assert worker.stopping

@pytest.mark.asyncio
async def test_worker_stops_after_shutdown_signal_while_idle(settings):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    queue = Mock()
    queue.ensure_group = AsyncMock()
    repo = Mock()
    processor = Mock()
    worker = DocumentWorker(settings, queue, repo, processor)

    async def read_then_stop(cursor, block_ms):
        worker.request_shutdown()
        return []

    queue.read = read_then_stop
    await asyncio.wait_for(worker.run_forever(), timeout=1)
    assert worker.stopping
