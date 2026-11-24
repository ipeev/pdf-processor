import pytest
from pypdf import PdfReader
from io import BytesIO

from app.errors import ProcessingError
from app.parsers.gemini_parser import GeminiPDFParser, split_one_page_pdf


class FakeGemini:
    def __init__(self):
        self.calls = []

    async def complete_pdf_page(self, payload, *, page, max_tokens):
        self.calls.append((page, payload, max_tokens))
        return f"# Page {page}\n\nExtracted content from page {page}."


def test_split_page_count(sample_basic):
    pages = split_one_page_pdf(sample_basic, 10)
    assert len(pages) == 2
    assert all(len(PdfReader(BytesIO(page)).pages) == 1 for page in pages)


def test_split_page_limit(sample_basic):
    with pytest.raises(ProcessingError, match="PAGE_LIMIT_EXCEEDED"):
        split_one_page_pdf(sample_basic, 1)


@pytest.mark.asyncio
async def test_two_pages_table(settings, sample_table):
    llm = FakeGemini()
    parsed = await GeminiPDFParser(llm, settings).parse(sample_table)
    assert len(parsed.pages) == 2
    assert [p.page for p in parsed.pages] == [1, 2]
    assert parsed.pages[0].markdown.startswith("# Page 1")
    assert [page for page, _, _ in llm.calls] == [1, 2]
    assert len(PdfReader(BytesIO(llm.calls[0][1])).pages) == 1


@pytest.mark.asyncio
async def test_page_limit_before_calls(settings, sample_blank):
    llm = FakeGemini()
    config = settings.model_copy(update={"max_ai_pages_per_document": 2})
    with pytest.raises(ProcessingError, match="AI_PAGE_LIMIT_EXCEEDED") as exc:
        await GeminiPDFParser(llm, config).parse(sample_blank)
    assert "3 pages" in exc.value.message
    assert "2 pages" in exc.value.message
    assert llm.calls == []


@pytest.mark.asyncio
async def test_blank_page_response_sentinel(settings, sample_blank):
    class BlankGemini(FakeGemini):
        async def complete_pdf_page(self, payload, *, page, max_tokens):
            self.calls.append((page, payload, max_tokens))
            return "__BLANK_PAGE__" if page == 2 else f"Page {page}"
    llm = BlankGemini()
    pages = (await GeminiPDFParser(llm, settings).parse(sample_blank)).pages
    assert len(pages) == 3
    assert pages[1].markdown == ""


@pytest.mark.asyncio
async def test_pdf_no_api_fallback(settings, sample_basic):
    class FailingGemini(FakeGemini):
        async def complete_pdf_page(self, payload, *, page, max_tokens):
            raise ProcessingError("PROVIDER_UNAVAILABLE", "Simulated upstream outage")
    with pytest.raises(ProcessingError, match="PROVIDER_UNAVAILABLE"):
        await GeminiPDFParser(FailingGemini(), settings).parse(sample_basic)


@pytest.mark.asyncio
async def test_native_pdf_reports_each_page_including_blanks(settings, sample_blank):
    reported = []
    async def report(page, total, attempt, maximum):
        reported.append((page, total, attempt, maximum))
    await GeminiPDFParser(FakeGemini(), settings).parse(sample_blank, on_progress=report)
    assert reported == [(1, 3, 1, 1), (2, 3, 1, 1), (3, 3, 1, 1)]
