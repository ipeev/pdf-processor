"""Render one PDF page at a time for the configurable image OCR model."""
import asyncio
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium

from app.config import Settings
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.page_cache import PageCache
from app.parsers.base import ParsedPage, ParsedDocument
from app.parsers.pdf_render import render_one_page
from app.parsers.pypdf_parser import safe_reader




class ImageOCRParser:
    def __init__(self, client: OpenRouterClient, settings: Settings, page_cache: PageCache | None = None):
        self.client, self.settings = client, settings
        self.page_cache = page_cache

    async def parse(self, path: Path, *, on_progress=None) -> ParsedDocument:
        # Check count/encryption with pypdf before opening rasterizer; no model calls yet.
        reader = await asyncio.to_thread(safe_reader, path, self.settings.max_pages_per_document)
        count = len(reader.pages)
        if count > self.settings.max_ai_pages_per_document:
            raise ProcessingError("AI_PAGE_LIMIT_EXCEEDED", f"This PDF has {count} pages; "
                                  f"the configured AI extraction limit is {self.settings.max_ai_pages_per_document} pages.")
        try:
            pdf = await asyncio.to_thread(pdfium.PdfDocument, str(path))
        except Exception as exc:
            raise ProcessingError("INVALID_PDF", "PDF cannot be rendered.") from exc
        try:
            if len(pdf) != count:
                raise ProcessingError("INVALID_PDF", "PDF page count changed unexpectedly.")
            pages: list[ParsedPage] = []
            for index in range(count):
                if on_progress is not None:
                    await on_progress(index + 1, count, 1, 1)
                try:
                    image = await asyncio.to_thread(render_one_page, pdf, index, self.settings)
                except ProcessingError as exc:
                    raise exc.with_context(phase="image_ocr", page=index + 1,
                                           pages_total=count) from None
                except Exception as exc:
                    raise ProcessingError("PAGE_RENDER_FAILED", "Unable to render PDF page.").with_context(
                        phase="image_ocr", page=index + 1, pages_total=count) from exc
                async def extract_page() -> str:
                    return await self.client.complete_image_page(
                        image, page=index + 1, max_tokens=self.settings.max_ai_output_tokens_per_page,
                    )
                try:
                    if self.page_cache is None:
                        text = await extract_page()
                    else:
                        text = await self.page_cache.get_or_extract(
                            image, parser="gpt5_mini", settings=self.settings,
                            page=index + 1, extract=extract_page,
                        )
                except ProcessingError as exc:
                    raise exc.with_context(phase="image_ocr", page=index + 1,
                                           pages_total=count) from None
                pages.append(ParsedPage(index + 1, "" if text.strip() == "__BLANK_PAGE__" else text))
            result = ParsedDocument(pages)
            result.validate()
            return result
        finally:
            pdf.close()


# Compatibility alias for existing imports/tests (API parser key remains "gpt5_mini").
GPT5OCRParser = ImageOCRParser
