"""Persist extraction before summary; support summary-only retry."""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any

from app.config import Settings
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.page_cache import PageCache
from app.parsers.pypdf_parser import PyPDFParser
from app.parsers.gemini_parser import NativePDFParser
from app.parsers.gpt5_parser import ImageOCRParser
from app.parsers.vision_figures import VisionFiguresParser
from app.parsers.base import ParsedDocument
from app.redis_store import DocumentRepository
from app.summarizer import DocumentSummarizer, SummaryProvider
from app.observability import span


class DocumentProcessor:
    def __init__(self, settings: Settings, repo: DocumentRepository,
                 llm: OpenRouterClient | None = None, summarizer: SummaryProvider | None = None,
                 parsers: dict[str, Any] | None = None):
        self.settings = settings
        self.repo = repo
        self.llm = llm
        self.summarizer = summarizer or (DocumentSummarizer(llm, settings) if llm else None)
        page_cache = PageCache(repo.redis, settings.cache_ttl_seconds) if llm else None
        self.parsers = parsers or {
            "pypdf": PyPDFParser(settings.max_pages_per_document),
            **({"gemini": NativePDFParser(llm, settings, page_cache),
                "gpt5_mini": ImageOCRParser(llm, settings, page_cache),
                "vision_figures": VisionFiguresParser(llm, settings)} if llm else {}),
        }

    async def extract(self, parser: str, path: Path, *, page_response=None):
        adapter = self.parsers.get(parser)
        if adapter is None:
            raise ProcessingError("UNAVAILABLE_PARSER", "Selected parser is not available.")
        with span("document.extract", **{"document.parser": parser}):
            if parser == "pypdf":
                result = await asyncio.to_thread(adapter.parse, path)
            elif parser == "vision_figures":
                result = await adapter.parse(path, page_response=page_response)
            else:
                result = await adapter.parse(path)
        if parser == "vision_figures":
            result.as_parsed_document().validate()
        else:
            result.validate()
        return result

    async def summarize_saved(self, doc_id, owner_token: str | None = None) -> None:
        parsed = await self.repo.read_extracted(doc_id)
        if self.summarizer is None:
            raise ProcessingError("SUMMARIZER_NOT_CONFIGURED", "Document summarizer is not configured.")
        with span("document.summarize"):
            summary = await self.summarizer.summarize(parsed.pages)
        await self.repo.store_completed(doc_id, parsed, summary, owner_token=owner_token)

    async def process(self, doc_id, parser: str, path: Path, owner_token: str | None = None) -> None:
        # If a worker crashed during summarization, don't repeat paid extraction.
        parsed = await self.repo.get_saved_extraction(doc_id)
        if parsed is None:
            if not path.is_file():
                raise ProcessingError("SOURCE_FILE_MISSING", "PDF source file is not accessible.")
            parsed = await self.extract(parser, path)
            await self.repo.store_extracted(doc_id, parsed, owner_token=owner_token)
        await self.summarize_saved(doc_id, owner_token=owner_token)
