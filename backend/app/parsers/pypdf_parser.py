"""Local text extraction; no claim to reconstruct rich Markdown/OCR."""
from pathlib import Path
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.errors import ProcessingError
from app.parsers.base import ParsedPage, ParsedDocument


def safe_reader(path: Path, max_pages: int) -> PdfReader:
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ProcessingError("PDF_ENCRYPTED", "Encrypted PDFs are not supported.")
        count = len(reader.pages)
        if count < 1:
            raise ProcessingError("INVALID_PDF", "PDF has no pages.")
        if count > max_pages:
            raise ProcessingError("PAGE_LIMIT_EXCEEDED", f"This PDF has {count} pages; the configured document limit is {max_pages} "
                                                        f"{'page' if max_pages == 1 else 'pages'}.")
        return reader
    except ProcessingError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        raise ProcessingError("INVALID_PDF", "PDF could not be read.") from exc


class PyPDFParser:
    def __init__(self, max_pages: int):
        self.max_pages = max_pages

    def parse(self, path: Path) -> ParsedDocument:
        reader = safe_reader(path, self.max_pages)
        pages: list[ParsedPage] = []
        try:
            for index, page in enumerate(reader.pages, start=1):
                pages.append(ParsedPage(page=index, markdown=page.extract_text() or ""))
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("INVALID_PDF", "Unable to extract PDF text.") from exc
        result = ParsedDocument(pages)
        result.validate()
        return result
