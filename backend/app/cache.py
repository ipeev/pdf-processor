"""Content-addressed document cache; no PDF binaries are stored in Redis.

Increment PROMPT_VERSION whenever extraction/summarization instructions change.
The digest includes all knobs that can change generated content.
"""
import hashlib
import json

from app.config import Settings
from app.schemas import ParserChoice

PROMPT_VERSION = "pdf-pipeline-v4"
PAGE_EXTRACTION_VERSION = "pdf-page-extraction-v2"


def fingerprint(file_sha256: str, parser: ParserChoice, settings: Settings) -> str:
    if len(file_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in file_sha256):
        raise ValueError("Invalid content digest")
    payload = {
        "pdf_sha256": file_sha256,
        "parser": parser.value,
        "prompt_version": PROMPT_VERSION,
        "native_pdf_model": settings.native_pdf_model,
        "image_ocr_model": settings.image_ocr_model,
        "summary_model": settings.summary_model,
        "max_pages": settings.max_pages_per_document,
        "max_ai_pages": settings.max_ai_pages_per_document,
        "max_summary_input_chars": settings.max_summary_input_chars,
        "max_ai_output_tokens": settings.max_ai_output_tokens_per_page,
        "max_summary_tokens": settings.max_summary_output_tokens,
        "render_scale": settings.render_scale,
        "max_render_pixels": settings.max_render_pixels,
        "max_render_image_bytes": settings.max_render_image_bytes,
    }
    if parser == ParserChoice.vision_figures:
        from app.parsers.vision_figures import PROMPT_VERSION as VISION_PROMPT_VERSION
        payload["vision_figures"] = {
            "prompt": VISION_PROMPT_VERSION,
            "model": settings.vision_figures_model,
            "output_tokens": settings.vision_figures_max_tokens_per_page,
            "max_pages": settings.max_vision_figures_pages_per_document,
            "max_figures": settings.max_vision_figures_per_page,
            "asset_bytes": settings.max_vision_asset_bytes,
            "total_asset_bytes": settings.max_vision_total_asset_bytes,
            "padding": settings.vision_figure_padding_norm,
        }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def result_key(digest: str) -> str:
    return f"pdfcache:v1:result:{digest}"


def active_key(digest: str) -> str:
    return f"pdfcache:v1:active:{digest}"


def page_fingerprint(content: bytes, parser: str, settings: Settings) -> str:
    """Hash the *exact model input* and settings relevant to page extraction.

    No document ID, page position, or summary model: identical extraction
    requests may be reused across different PDFs. Different formats/models
    and extraction settings cannot share results.
    """
    if parser not in {"gemini", "gpt5_mini"}:
        raise ValueError("Page caching is available only for AI extraction")
    payload = {
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "parser": parser,
        "extraction_version": PAGE_EXTRACTION_VERSION,
        "model": settings.native_pdf_model if parser == "gemini" else settings.image_ocr_model,
        "max_output_tokens": settings.max_ai_output_tokens_per_page,
    }
    if parser == "gemini":
        payload["pdf_engine"] = "native"
    else:
        payload.update({
            "render_scale": settings.render_scale,
            "max_render_pixels": settings.max_render_pixels,
            "max_render_image_bytes": settings.max_render_image_bytes,
        })
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def page_key(digest: str) -> str:
    return f"pdfcache:v1:page:{digest}"
