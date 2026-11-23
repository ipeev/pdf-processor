"""v0.11.5: cross-document AI page reuse, isolation, failures, and TTL."""
from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader, PdfWriter
from redis.exceptions import ConnectionError as RedisConnectionError

from app.cache import page_fingerprint, page_key
from app.config import Settings
from app.document_processor import DocumentProcessor
from app.errors import ProcessingError
from app.main import app
from app.object_store import LocalTestObjectStore
from app.page_cache import PageCache
from app.parsers.gemini_parser import NativePDFParser
from app.parsers.gpt5_parser import ImageOCRParser
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.worker import DocumentWorker
from tests.fake_redis import FakeRedis


def make_pdf(path: Path, pages: list) -> Path:
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    with path.open("wb") as file:
        writer.write(file)
    return path


class ContentLLM:
    """Stable Markdown for equivalent inputs, regardless of source page index."""
    def __init__(self):
        self.pdf_calls: list[tuple[int, str]] = []
        self.image_calls: list[tuple[int, str]] = []
        self.fail_pdf_page: int | None = None

    async def complete_pdf_page(self, content, *, page, max_tokens):
        self.pdf_calls.append((page, hashlib.sha256(content).hexdigest()))
        if self.fail_pdf_page == page:
            raise ProcessingError("PROVIDER_UNAVAILABLE", "Simulated page failure")
        return "Extracted " + hashlib.sha256(content).hexdigest()

    async def complete_image_page(self, content, *, page, max_tokens):
        self.image_calls.append((page, hashlib.sha256(content).hexdigest()))
        return "OCR " + hashlib.sha256(content).hexdigest()


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    async def summarize(self, pages):
        self.calls += 1
        return "Summary of " + str(len(pages)) + " pages"


def test_page_fingerprint_is_task_specific(settings):
    payload = b"a single PDF page"
    original = page_fingerprint(payload, "gemini", settings)
    assert len(original) == 64
    assert original == page_fingerprint(payload, "gemini", settings)
    assert original != page_fingerprint(b"modified page", "gemini", settings)
    assert original != page_fingerprint(payload, "gpt5_mini", settings)
    assert original != page_fingerprint(payload, "gemini", settings.model_copy(update={
        "native_pdf_model": "new/model"}))
    assert original != page_fingerprint(payload, "gemini", settings.model_copy(update={
        "max_ai_output_tokens_per_page": 1024}))
    assert original == page_fingerprint(payload, "gemini", settings.model_copy(update={
        "summary_model": "different/summary-model", "max_summary_input_chars": 1,
        "max_summary_output_tokens": 10, "render_scale": 2.0,
    }))
    image = page_fingerprint(payload, "gpt5_mini", settings)
    assert image != page_fingerprint(payload, "gpt5_mini", settings.model_copy(update={
        "render_scale": 2.0}))
    assert image != page_fingerprint(payload, "gpt5_mini", settings.model_copy(update={
        "image_ocr_model": "new/ocr-model"}))
    with pytest.raises(ValueError):
        page_fingerprint(payload, "pypdf", settings)


@pytest.mark.asyncio
async def test_native_page_reused_across_reordered_documents(settings, sample_table, tmp_path):
    source = PdfReader(str(sample_table))
    first = make_pdf(tmp_path / "A.pdf", [source.pages[0], source.pages[1]])
    second = make_pdf(tmp_path / "B.pdf", [source.pages[1], source.pages[0]])
    assert first.read_bytes() != second.read_bytes()
    model = ContentLLM()
    redis = FakeRedis()
    parser = NativePDFParser(model, settings, PageCache(redis, settings.cache_ttl_seconds))
    a = await parser.parse(first)
    assert len(model.pdf_calls) == 2
    b = await parser.parse(second)
    assert len(model.pdf_calls) == 2  # both pages came from cache, not OpenRouter
    assert b.pages[0].markdown == a.pages[1].markdown
    assert b.pages[1].markdown == a.pages[0].markdown
    assert len([key for key in redis.keys if key.startswith("pdfcache:v1:page:")]) == 2


@pytest.mark.asyncio
async def test_image_page_reused_when_added_to_another_pdf(settings, sample_scanned, sample_basic, tmp_path):
    scan_page = PdfReader(str(sample_scanned)).pages[0]
    text_page = PdfReader(str(sample_basic)).pages[0]
    a = make_pdf(tmp_path / "scan.pdf", [scan_page])
    b = make_pdf(tmp_path / "mixed.pdf", [text_page, scan_page])
    model = ContentLLM()
    parser = ImageOCRParser(model, settings, PageCache(FakeRedis(), settings.cache_ttl_seconds))
    first = await parser.parse(a)
    assert len(model.image_calls) == 1
    second = await parser.parse(b)
    assert len(model.image_calls) == 2  # one new page, one cache hit
    assert second.pages[1].markdown == first.pages[0].markdown
    assert model.image_calls[-1][0] == 1


@pytest.mark.asyncio
async def test_failure_does_not_evict_earlier_pages(settings, sample_table, tmp_path):
    model = ContentLLM()
    model.fail_pdf_page = 2
    redis = FakeRedis()
    parser = NativePDFParser(model, settings, PageCache(redis, settings.cache_ttl_seconds))
    with pytest.raises(ProcessingError, match="PROVIDER_UNAVAILABLE") as error:
        await parser.parse(sample_table)
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert len(model.pdf_calls) == 2
    assert len([key for key in redis.keys if key.startswith("pdfcache:v1:page:")]) == 1
    model.fail_pdf_page = None
    extracted = await parser.parse(sample_table)
    assert len(extracted.pages) == 2
    assert len(model.pdf_calls) == 3  # page 1 hit; only previously failed page reprocessed


@pytest.mark.asyncio
async def test_blank_sentinel_cached_but_error_is_not(settings):
    redis = FakeRedis()
    cache = PageCache(redis, settings.cache_ttl_seconds)
    calls = 0

    async def blank():
        nonlocal calls
        calls += 1
        return "__BLANK_PAGE__"

    assert await cache.get_or_extract(b"blank", parser="gemini", settings=settings,
                                      page=1, extract=blank) == ""
    assert await cache.get_or_extract(b"blank", parser="gemini", settings=settings,
                                      page=5, extract=blank) == ""
    assert calls == 1
    digest = page_fingerprint(b"blank", "gemini", settings)
    assert await redis.get(page_key(digest)) == ""

    async def fail():
        raise ProcessingError("PROVIDER_UNAVAILABLE", "Failure")

    with pytest.raises(ProcessingError):
        await cache.get_or_extract(b"failure", parser="gemini", settings=settings,
                                   page=1, extract=fail)
    assert await redis.get(page_key(page_fingerprint(b"failure", "gemini", settings))) is None


@pytest.mark.asyncio
async def test_page_ttl_24h_and_expiry(settings):
    assert settings.cache_ttl_seconds == 86400
    assert settings.result_ttl_seconds == 86400
    redis = FakeRedis()
    cache = PageCache(redis, settings.cache_ttl_seconds)
    calls = 0

    async def extract():
        nonlocal calls
        calls += 1
        return "Valid text"

    params = dict(parser="gemini", settings=settings, page=4, extract=extract)
    assert await cache.get_or_extract(b"payload", **params) == "Valid text"
    key = page_key(page_fingerprint(b"payload", "gemini", settings))
    assert 86399 <= await redis.ttl(key) <= 86400
    current = redis.clock()
    redis.clock = lambda: current + 86399
    assert await cache.get_or_extract(b"payload", **params) == "Valid text"
    assert calls == 1
    redis.clock = lambda: current + 86401
    assert await cache.get_or_extract(b"payload", **params) == "Valid text"
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_unavailability_does_not_prevent_ai_extraction(settings):
    class UnavailableRedis:
        async def get(self, *a):
            raise RedisConnectionError("not reachable")
        async def set(self, *a, **kw):
            raise RedisConnectionError("not reachable")

    calls = 0
    async def extract():
        nonlocal calls
        calls += 1
        return "Extracted text"

    cache = PageCache(UnavailableRedis(), 86400)
    assert await cache.get_or_extract(b"a", parser="gemini", settings=settings,
                                      page=1, extract=extract) == "Extracted text"
    assert calls == 1


@pytest.mark.asyncio
async def test_ui_upload_second_pdf_reuses_pages_but_resummarizes(settings, sample_table, tmp_path):
    """Exercise the API upload->queue->worker->parser chain without paid calls."""
    pages = PdfReader(str(sample_table)).pages
    a = make_pdf(tmp_path / "uploaded_A.pdf", [pages[0], pages[1]])
    b = make_pdf(tmp_path / "uploaded_B.pdf", [pages[1], pages[0]])
    redis = FakeRedis()
    repo = DocumentRepository(redis, prefix="page-cache-end-to-end", result_ttl=settings.result_ttl_seconds,
                              cache_ttl=settings.cache_ttl_seconds)
    queue = DocumentQueue(redis, "page-cache-end-to-end:stream", "page-cache-end-to-end:workers", "w1")
    store = LocalTestObjectStore(settings.upload_dir)
    app.state.settings = settings
    app.state.redis = redis
    app.state.repository = repo
    app.state.queue = queue
    app.state.object_store = store
    model, summarizer = ContentLLM(), FakeSummarizer()
    worker = DocumentWorker(settings, queue, repo,
                            DocumentProcessor(settings, repo, llm=model, summarizer=summarizer),
                            object_store=store)
    await queue.ensure_group()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        ids = []
        for file in (a, b):
            response = await http.post("/api/v1/documents", data={"parser": "gemini"},
                                       files={"files": (file.name, file.read_bytes(), "application/pdf")})
            assert response.status_code == 202, response.text
            entry = response.json()["documents"][0]
            assert entry["cache"] == "miss"  # different document hashes
            ids.append(entry["id"])
            await worker.handle_message(*(await queue.read(">"))[0])
            status = (await http.get(f"/api/v1/documents/{entry['id']}")).json()
            assert status["status"] == "completed", status
        assert len(model.pdf_calls) == 2  # two initial AI calls, zero for second document
        assert summarizer.calls == 2       # each distinct document gets its own summary
        assert ids[0] != ids[1]
        result_1 = (await http.get(f"/api/v1/documents/{ids[0]}/pages")).json()["pages"]
        result_2 = (await http.get(f"/api/v1/documents/{ids[1]}/pages")).json()["pages"]
        assert result_2[0]["markdown"] == result_1[1]["markdown"]
        assert result_2[1]["markdown"] == result_1[0]["markdown"]

@pytest.mark.asyncio
@pytest.mark.parametrize("method,payload", [
    ("complete_pdf_page", b"%PDF-test-page"),
    ("complete_image_page", b"\x89PNG-test-page"),
])
async def test_provider_extraction_request_is_position_independent(settings, method, payload):
    """The model must see the same request for a cached page at any index."""
    import json
    from app.openrouter_client import OpenRouterClient

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": "markdown"}
        }]})

    settings = settings.model_copy(update={
        "openrouter_api_key": Settings(_env_file=None, openrouter_api_key="offline-test-key").openrouter_api_key
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(settings, http)
        assert await getattr(client, method)(payload, page=1, max_tokens=50) == "markdown"
        assert await getattr(client, method)(payload, page=20, max_tokens=50) == "markdown"
    assert requests[0] == requests[1]
    if method == "complete_pdf_page":
        assert requests[0]["messages"][1]["content"][1]["file"]["filename"] == "page.pdf"
