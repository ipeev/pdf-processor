"""Parser-independent output contracts, with explicit page cardinality checks."""
from dataclasses import dataclass

from app.errors import ProcessingError, no_text


@dataclass(frozen=True)
class ParsedPage:
    page: int
    markdown: str


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[ParsedPage]

    def validate(self) -> None:
        if not self.pages:
            raise ProcessingError("INVALID_PDF", "Document has no pages.")
        if [p.page for p in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ProcessingError("INVALID_PAGE_ORDER", "Extracted page indexes are invalid.")
        if any(not isinstance(p.markdown, str) for p in self.pages):
            raise ProcessingError("INVALID_PAGE_CONTENT", "Extracted content is not text.")
        if not any(p.markdown.strip() for p in self.pages):
            raise no_text()
