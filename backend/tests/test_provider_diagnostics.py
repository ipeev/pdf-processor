"""Privacy-safe context survives model -> parser -> Redis -> status API."""
import json
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.parsers.gemini_parser import NativePDFParser
from app.parsers.gpt5_parser import ImageOCRParser
from app.provider_diagnostics import sanitize_diagnostic
from app.redis_store import DocumentRepository
from tests.fake_redis import FakeRedis


def config():
    return Settings(_env_file=None, openrouter_api_key="test-key")


@pytest.mark.asyncio
async def test_truncation_has_page_model_tokens_usage_and_response_id():
    def handler(req):
        return httpx.Response(200, json={
            "id": "gen-123", "model": "upstream/served-model",
            "usage": {"prompt_tokens": 120, "completion_tokens": 32768, "total_tokens": 32888},
            "choices": [{"finish_reason": "length", "native_finish_reason": "max_tokens",
                         "message": {"content": "never reveal this content"}}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(ProcessingError) as caught:
            await OpenRouterClient(config(), transport).complete_image_page(b"SECRET IMAGE", page=4, max_tokens=32768)
    error = caught.value
    assert error.code == "PROVIDER_TRUNCATED"
    assert error.diagnostic == {
        "phase": "image_ocr", "kind": "finish", "requested_model": "deepseek/deepseek-v4.1-flash-1017",
        "response_model": "upstream/served-model", "finish_reason": "length",
        "native_finish_reason": "max_tokens", "response_id": "gen-123",
        "page": 4, "max_tokens": 32768, "attempt": 1, "max_attempts": 3,
        "prompt_tokens": 120, "completion_tokens": 32768, "total_tokens": 32888,
        "http_status": 200,
    }
    assert "SECRET" not in str(error.diagnostic)
    assert "never reveal" not in str(error.diagnostic)


@pytest.mark.asyncio
async def test_unexpected_finish_reason_preserved_and_injection_dropped():
    def handler(req):
        return httpx.Response(200, json={
            "id": "unsafe\nAPI_KEY=SECRET", "model": "test\nsecret",
            "choices": [{"finish_reason": "content_filter", "native_finish_reason": "<exfil>"}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(ProcessingError) as caught:
            await OpenRouterClient(config(), transport).complete_pdf_page(b"%PDF", page=2, max_tokens=100)
    assert caught.value.code == "PROVIDER_INCOMPLETE"
    d = caught.value.diagnostic
    assert d["finish_reason"] == "content_filter"
    assert d["phase"] == "ai_pdf"
    assert d["page"] == 2
    assert "response_id" not in d and "response_model" not in d
    assert "native_finish_reason" not in d


@pytest.mark.asyncio
async def test_missing_finish_reason_is_not_made_up():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "unfinished"}}]})
    )) as transport:
        with pytest.raises(ProcessingError) as caught:
            await OpenRouterClient(config(), transport).complete_text("short", max_tokens=10)
    assert caught.value.code == "PROVIDER_INCOMPLETE"
    assert "finish_reason" not in caught.value.diagnostic
    assert caught.value.diagnostic["phase"] == "summarization"
    assert "page" not in caught.value.diagnostic


@pytest.mark.asyncio
async def test_http_429_is_bounded_and_records_retry_count(monkeypatch):
    import asyncio
    async def no_wait(delay):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    attempts = 0
    def handler(req):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": "DO NOT STORE SECRET"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(ProcessingError) as caught:
            await OpenRouterClient(config(), transport).complete_text("short", max_tokens=4096)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.diagnostic["http_status"] == 429
    assert caught.value.diagnostic["attempt"] == 3
    assert caught.value.diagnostic["max_attempts"] == 3
    assert "SECRET" not in str(caught.value.diagnostic)
    assert attempts == 3


@pytest.mark.asyncio
async def test_timeout_has_no_invented_http_status():
    def handler(req):
        raise httpx.ReadTimeout("PRIVATE HEADER CONTENT")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(ProcessingError) as caught:
            await OpenRouterClient(config(), transport).complete_text("short", max_tokens=4096)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.diagnostic["kind"] == "timeout"
    assert "http_status" not in caught.value.diagnostic
    assert "PRIVATE" not in str(caught.value.diagnostic)


@pytest.mark.asyncio
async def test_pdf_page_attribution(settings, sample_table):
    class BreakOnSecond:
        async def complete_pdf_page(self, payload, *, page, max_tokens):
            if page == 2:
                raise ProcessingError("PROVIDER_INCOMPLETE", "Not finished.").with_context(
                    finish_reason="content_filter", requested_model="safe/model")
            return "OK"
    with pytest.raises(ProcessingError) as caught:
        await NativePDFParser(BreakOnSecond(), settings).parse(sample_table)
    assert caught.value.diagnostic["phase"] == "ai_pdf"
    assert caught.value.diagnostic["page"] == 2
    assert caught.value.diagnostic["pages_total"] == 2
    assert caught.value.diagnostic["finish_reason"] == "content_filter"


@pytest.mark.asyncio
async def test_ocr_page_attribution(settings, sample_scanned):
    class BreakOCR:
        async def complete_image_page(self, payload, *, page, max_tokens):
            raise ProcessingError("PROVIDER_TRUNCATED", "Too long.")
    with pytest.raises(ProcessingError) as caught:
        await ImageOCRParser(BreakOCR(), settings).parse(sample_scanned)
    assert caught.value.diagnostic["phase"] == "image_ocr"
    assert caught.value.diagnostic["page"] == 1
    assert caught.value.diagnostic["pages_total"] == 1


@pytest.mark.asyncio
async def test_failed_and_partial_diagnostics_persist_and_clear(redis_fake):
    from app.parsers.base import ParsedDocument, ParsedPage
    repo = DocumentRepository(redis_fake)
    doc = uuid4()
    await redis_fake.hset(repo.key(doc, "meta"), mapping=repo.prepare_meta(doc, "report.pdf", "gemini"))
    error = ProcessingError("PROVIDER_INCOMPLETE", "Incomplete.").with_context(
        phase="ai_pdf", page=3, pages_total=20, finish_reason="content_filter",
        unsafe_blob="SECRET PHRASE")
    await repo.store_failed(doc, error)
    meta = await repo.get_meta(doc)
    assert json.loads(meta["error_diagnostic"])["page"] == 3
    assert "SECRET" not in meta["error_diagnostic"]
    await repo.mark_processing(doc)
    assert (await repo.get_meta(doc))["error_diagnostic"] == ""
    await repo.store_extracted(doc, ParsedDocument([ParsedPage(1, "ok")]))
    await repo.store_partial(doc, error)
    assert json.loads((await repo.get_meta(doc))["error_diagnostic"])["pages_total"] == 20
    await repo.mark_summarizing(doc)
    assert (await repo.get_meta(doc))["error_diagnostic"] == ""
    await repo.store_completed(doc, ParsedDocument([ParsedPage(1, "ok")]), "A summary")
    assert (await repo.get_meta(doc))["error_diagnostic"] == ""


@pytest.mark.asyncio
async def test_api_exposes_allowlisted_details_and_old_records(settings):
    # Never mutate the singleton production FastAPI app between tests.
    from fastapi import FastAPI
    from app.api import router
    app = FastAPI()
    app.include_router(router)
    fake = FakeRedis()
    app.state.redis = fake
    app.state.settings = settings
    repo = DocumentRepository(fake)
    app.state.repository = repo
    from app.queue import DocumentQueue
    app.state.queue = DocumentQueue(fake, "jobs", "workers", "test-consumer")
    doc = uuid4()
    await fake.hset(repo.key(doc, "meta"), mapping=repo.prepare_meta(doc, "test.pdf", "gpt5_mini"))
    await repo.store_failed(doc, ProcessingError("PROVIDER_TRUNCATED", "Token limit.").with_context(
        phase="image_ocr", page=5, pages_total=35, finish_reason="length", max_tokens=32768))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get(f"/api/v1/documents/{doc}")
        assert status.status_code == 200
        assert status.json()["error"]["diagnostic"]["page"] == 5
        assert status.json()["error"]["diagnostic"]["finish_reason"] == "length"
        # Old failed records with no diagnostics remain valid.
        other = uuid4()
        meta = repo.prepare_meta(other, "old.pdf", "pypdf")
        meta.update(status="failed", error_code="INVALID_PDF", error_message="Unable to read")
        del meta["error_diagnostic"]
        await fake.hset(repo.key(other, "meta"), mapping=meta)
        old = await client.get(f"/api/v1/documents/{other}")
        assert old.status_code == 200
        assert old.json()["error"]["diagnostic"] is None


def test_sanitizer_excludes_secrets_and_oversized_values():
    result = sanitize_diagnostic({
        "requested_model": "good/model", "response_id": "x" * 121,
        "finish_reason": "openrouter-key-injection\nSECRET", "raw_body": "SECRET",
        "prompt": "DO NOT SHOW", "max_tokens": 32768, "attempt": True,
        "http_status": -1,
    })
    assert result == {"requested_model": "good/model", "max_tokens": 32768}
