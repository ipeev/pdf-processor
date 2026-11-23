from io import BytesIO

import pytest
import pypdfium2 as pdfium
from PIL import Image

from app.errors import ProcessingError
from app.parsers.gpt5_parser import GPT5OCRParser, render_one_page


class FakeGPT:
    def __init__(self):
        self.calls = []

    async def complete_image_page(self, payload, *, page, max_tokens):
        self.calls.append((page, payload, max_tokens))
        return "# SCANNED BLUE ORCHID\n\nThis document has no selectable text layer."


def test_render_one_page(settings, sample_scanned):
    pdf = pdfium.PdfDocument(str(sample_scanned))
    try:
        encoded = render_one_page(pdf, 0, settings)
    finally:
        pdf.close()
    image = Image.open(BytesIO(encoded))
    assert image.format == "PNG"
    assert image.width > 400 and image.height > 500
    image.close()


def test_page_render_limit(settings, sample_scanned):
    cfg = settings.model_copy(update={"max_render_pixels": 100})
    pdf = pdfium.PdfDocument(str(sample_scanned))
    try:
        with pytest.raises(ProcessingError, match="PAGE_RENDER_LIMIT_EXCEEDED"):
            render_one_page(pdf, 0, cfg)
    finally:
        pdf.close()


@pytest.mark.asyncio
async def test_scanned_ocr(settings, sample_scanned):
    fake = FakeGPT()
    parsed = await GPT5OCRParser(fake, settings).parse(sample_scanned)
    assert len(parsed.pages) == 1
    assert parsed.pages[0].page == 1
    assert "SCANNED BLUE ORCHID" in parsed.pages[0].markdown
    assert fake.calls[0][1].startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_ai_page_limit_before_calls(settings, sample_blank):
    cfg = settings.model_copy(update={"max_ai_pages_per_document": 2})
    fake = FakeGPT()
    with pytest.raises(ProcessingError, match="AI_PAGE_LIMIT_EXCEEDED") as exc:
        await GPT5OCRParser(fake, cfg).parse(sample_blank)
    assert "3 pages" in exc.value.message
    assert "2 pages" in exc.value.message
    assert fake.calls == []


@pytest.mark.asyncio
async def test_ocr_provider_failure_not_silent(settings, sample_scanned):
    class FailGPT(FakeGPT):
        async def complete_image_page(self, payload, *, page, max_tokens):
            raise ProcessingError("PROVIDER_UNAVAILABLE", "Upstream unavailable")
    with pytest.raises(ProcessingError, match="PROVIDER_UNAVAILABLE"):
        await GPT5OCRParser(FailGPT(), settings).parse(sample_scanned)
