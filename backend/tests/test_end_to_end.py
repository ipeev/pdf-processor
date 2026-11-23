"""Offline E2E exercises all processors and Redis-state contracts with a test double."""
from uuid import uuid4

import pytest

from app.document_processor import DocumentProcessor
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import ParserChoice
from app.storage import StagedFile
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis
from tests.conftest import SAMPLES_DIR


class OfflineModel:
    def __init__(self):
        self.summary_calls = 0
        self.pdf_calls = 0
        self.image_calls = 0

    async def complete_text(self, prompt: str, *, max_tokens: int,
                            system_prompt: str | None = None) -> str:
        assert system_prompt is not None
        self.summary_calls += 1
        assert "Original page" in prompt
        return "One overall Gemini-style summary."

    async def complete_pdf_page(self, pdf_bytes: bytes, *, page: int, max_tokens: int) -> str:
        self.pdf_calls += 1
        assert pdf_bytes.startswith(b"%PDF-")
        return f"## Page {page}\n\nRelevant content"

    async def complete_image_page(self, png_bytes: bytes, *, page: int, max_tokens: int) -> str:
        self.image_calls += 1
        assert png_bytes.startswith(b"\x89PNG")
        return f"## OCR page {page}\n\nSCANNED BLUE ORCHID"


@pytest.mark.asyncio
@pytest.mark.parametrize("parser,fixture", [
    (ParserChoice.pypdf, "sample_basic"),
    (ParserChoice.gemini, "sample_table"),
    (ParserChoice.gpt5_mini, "sample_scanned"),
])
async def test_three_parsers_with_one_gemini_summary(parser, fixture, request, settings):
    sample = request.getfixturevalue(fixture)
    doc_id = uuid4()
    target = settings.upload_dir / f"{doc_id}.pdf"
    target.write_bytes(sample.read_bytes())
    redis = FakeRedis()
    repo = DocumentRepository(redis, "e2e")
    queue = DocumentQueue(redis, "e2e:jobs", "e2e-group", "e2e-worker")
    await queue.ensure_group()
    doc = StagedFile(doc_id, sample.name, parser, target, target, target.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    model = OfflineModel()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, llm=model))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc_id))["status"] == "completed"
    pages, summary = await repo.read_completed(doc_id)
    assert len(pages) == (1 if parser == ParserChoice.gpt5_mini else 2)
    assert summary.startswith("One overall Gemini-style")
    assert model.summary_calls == 1
    assert model.pdf_calls == (2 if parser == ParserChoice.gemini else 0)
    assert model.image_calls == (1 if parser == ParserChoice.gpt5_mini else 0)
    assert (await redis.xpending(queue.stream, queue.group))["pending"] == 0


@pytest.mark.asyncio
async def test_failed_pdf_never_gets_summary(settings, sample_scanned):
    doc_id = uuid4()
    target = settings.upload_dir / f"{doc_id}.pdf"
    target.write_bytes(sample_scanned.read_bytes())
    redis = FakeRedis()
    repo = DocumentRepository(redis)
    queue = DocumentQueue(redis, "scan:test", "scan-group", "consumer")
    await queue.ensure_group()
    doc = StagedFile(doc_id, "scan.pdf", ParserChoice.pypdf, target, target, target.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    model = OfflineModel()
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, llm=model))
    await worker.handle_message(*(await queue.read(">"))[0])
    assert (await repo.get_meta(doc_id))["error_code"] == "NO_EXTRACTABLE_TEXT"
    assert model.summary_calls == 0


@pytest.mark.asyncio
async def test_non_summary_is_a_failed_job_not_completed(settings, sample_scanned):
    """A live-style misleading refusal must not be published as a completed summary."""
    class UnusableSummaryLLM:
        async def complete_text(self, prompt, *, max_tokens, system_prompt=None):
            return "Please provide the content of the document or upload the file."

    doc_id = uuid4()
    target = settings.upload_dir / f"{doc_id}.pdf"
    # Use text PDF so summary generation occurs after successful extraction.
    # The refused summary must leave extracted pages accessible as partial results.
    target.write_bytes((SAMPLES_DIR / "basic_text.pdf").read_bytes())
    redis = FakeRedis()
    repo = DocumentRepository(redis, "refusal")
    queue = DocumentQueue(redis, "refusal:jobs", "refusal-group", "refusal-worker")
    await queue.ensure_group()
    doc = StagedFile(doc_id, "basic_text.pdf", ParserChoice.pypdf,
                     target, target, target.stat().st_size)
    await queue.enqueue_batch([doc], repo)
    worker = DocumentWorker(settings, queue, repo, DocumentProcessor(settings, repo, llm=UnusableSummaryLLM()))
    await worker.handle_message(*(await queue.read(">"))[0])
    meta = await repo.get_meta(doc_id)
    assert meta["status"] == "partial"
    assert meta["error_code"] == "SUMMARY_UNUSABLE"
    assert await redis.get(repo.key(doc_id, "summary")) is None
    assert len((await repo.read_extracted(doc_id)).pages) == 2
    assert (await redis.xpending(queue.stream, queue.group))["pending"] == 0
