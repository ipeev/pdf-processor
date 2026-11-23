"""Shared bounded PDFium page renderer for OCR and figure extraction."""
from io import BytesIO
import pypdfium2 as pdfium
from app.config import Settings
from app.errors import ProcessingError

def render_one_page(pdf: pdfium.PdfDocument, index: int, settings: Settings) -> bytes:
    page = pdf[index]
    try:
        width, height = page.get_size()
        pixels = int(width * settings.render_scale + 1) * int(height * settings.render_scale + 1)
        if pixels > settings.max_render_pixels:
            raise ProcessingError("PAGE_RENDER_LIMIT_EXCEEDED", "Rendered page exceeds pixel budget.")
        bitmap = page.render(scale=settings.render_scale)
        try:
            image = bitmap.to_pil()
            try:
                if image.width * image.height > settings.max_render_pixels:
                    raise ProcessingError("PAGE_RENDER_LIMIT_EXCEEDED", "Rendered page exceeds pixel budget.")
                output = BytesIO()
                image.save(output, format="PNG", optimize=True)
                data = output.getvalue()
                if len(data) > settings.max_render_image_bytes:
                    raise ProcessingError("PAGE_RENDER_LIMIT_EXCEEDED", "Rendered page exceeds image byte budget.")
                return data
            finally:
                image.close()
        finally:
            bitmap.close()
    finally:
        page.close()

