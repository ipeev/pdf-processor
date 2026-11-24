"""Configurable native-PDF model extraction (legacy public parser ID: gemini)."""
import asyncio
from io import BytesIO
from pathlib import Path
from pypdf import PdfWriter

from app.config import Settings
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.page_cache import PageCache
from app.parsers.base import ParsedPage, ParsedDocument
from app.parsers.pypdf_parser import safe_reader


def is_objectively_blank(page) -> bool:
    """Only consider missing/empty page content blank; do not misclassify scans."""
    contents = page.get_contents()
    if contents is not None:
        try:
            if contents.get_data().strip():
                return False
        except Exception:
            return False
    # Page with image or resources should still go to multimodal model.
    res = page.get("/Resources")
    if res:
        try:
            dictionary = res.get_object()
            if dictionary.get("/XObject"):
                return False
        except Exception:
            return False
    return True


def split_one_page_pdf(path: Path, max_pages: int) -> list[bytes]:
    reader = safe_reader(path, max_pages)
    results = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        results.append(output.getvalue())
    return results


class NativePDFParser:
    def __init__(self, client: OpenRouterClient, settings: Settings, page_cache: PageCache | None = None):
        self.client, self.settings = client, settings
        self.page_cache = page_cache

    async def parse(self, path: Path, *, on_progress=None) -> ParsedDocument:
        reader = await asyncio.to_thread(safe_reader, path, self.settings.max_pages_per_document)
        if len(reader.pages) > self.settings.max_ai_pages_per_document:
            raise ProcessingError("AI_PAGE_LIMIT_EXCEEDED", f"This PDF has {len(reader.pages)} pages; "
                                  f"the configured AI extraction limit is {self.settings.max_ai_pages_per_document} pages.")
        parsed = []
        for index, page in enumerate(reader.pages, start=1):
            if on_progress is not None:
                await on_progress(index, len(reader.pages), 1, 1)
            if is_objectively_blank(page):
                parsed.append(ParsedPage(index, ""))
                continue
            def create_page() -> bytes:
                writer = PdfWriter()
                writer.add_page(page)
                output = BytesIO()
                writer.write(output)
                return output.getvalue()
            pdf_bytes = await asyncio.to_thread(create_page)
            async def extract_page() -> str:
                return await self.client.complete_pdf_page(
                    pdf_bytes, page=index, max_tokens=self.settings.max_ai_output_tokens_per_page,
                )
            try:
                if self.page_cache is None:
                    text = await extract_page()
                else:
                    text = await self.page_cache.get_or_extract(
                        pdf_bytes, parser="gemini", settings=self.settings,
                        page=index, extract=extract_page,
                    )
            except ProcessingError as exc:
                raise exc.with_context(phase="ai_pdf", page=index,
                                       pages_total=len(reader.pages)) from None
            parsed.append(ParsedPage(index, "" if text.strip() == "__BLANK_PAGE__" else text))
        result = ParsedDocument(parsed)
        result.validate()
        return result


# Compatibility alias for existing imports/tests (API parser key remains "gemini").
GeminiPDFParser = NativePDFParser
