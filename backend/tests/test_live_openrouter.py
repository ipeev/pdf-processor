"""Paid probes: RUN_LIVE_TESTS=1 and a funded OPENROUTER_API_KEY are mandatory."""
import pytest

from app.config import Settings
from app.openrouter_client import OpenRouterClient
from app.parsers.gemini_parser import NativePDFParser
from app.parsers.gpt5_parser import ImageOCRParser
from app.summarizer import DocumentSummarizer
from tests.conftest import SAMPLES_DIR


def configured_settings():
    config = Settings()
    if not config.openrouter_api_key.get_secret_value():
        pytest.skip("Set OPENROUTER_API_KEY in .env before paid probe")
    return config


@pytest.mark.live
@pytest.mark.asyncio
async def test_summary_model_text():
    config = configured_settings()
    llm = OpenRouterClient(config)
    try:
        # Smoke-test the configured summary model with its actual production output cap.
        # Reasoning-capable models may consume a 100-token budget before answering.
        text = await llm.complete_text(
            "Reply with the exact phrase: TEST ONLY ALPHA",
            max_tokens=config.max_summary_output_tokens,
        )
        assert "TEST ONLY ALPHA" in text.upper()
    finally:
        await llm.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_native_pdf_model():
    config = configured_settings()
    llm = OpenRouterClient(config)
    path = SAMPLES_DIR / "basic_text.pdf"
    try:
        parsed = await NativePDFParser(llm, config).parse(path)
        assert len(parsed.pages) == 2
        assert "river" in parsed.pages[0].markdown.lower()
    finally:
        await llm.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_image_ocr_and_summary_model():
    config = configured_settings()
    llm = OpenRouterClient(config)
    path = SAMPLES_DIR / "scanned_image_only.pdf"
    try:
        parsed = await ImageOCRParser(llm, config).parse(path)
        assert "BLUE ORCHID" in parsed.pages[0].markdown.upper()
        summary = await DocumentSummarizer(llm, config).summarize(parsed.pages)
        assert "BLUE ORCHID" in summary.upper(), ("OCR was extracted but the configured summary model did not summarize its subject: " + summary)
    finally:
        await llm.aclose()
