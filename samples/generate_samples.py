"""Generate deterministic synthetic, non-private demo PDFs."""
from pathlib import Path
from tempfile import TemporaryDirectory
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent


def page_doc(name, page_items):
    pdf = canvas.Canvas(str(HERE / name), pagesize=(595, 842), pageCompression=1)
    for lines in page_items:
        pdf.setFont("Helvetica", 12)
        for n, text in enumerate(lines):
            pdf.drawString(55, 780 - n * 27, text)
        pdf.showPage()
    pdf.save()


def scan_doc():
    image = Image.new("RGB", (1250, 1750), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font = ImageFont.truetype(font_path, 44) if Path(font_path).exists() else ImageFont.load_default()
    draw.text((95, 120), "SCANNED BLUE ORCHID", fill="black", font=font)
    draw.text((95, 250), "This document has no selectable text layer.", fill="black", font=font)
    pdf = canvas.Canvas(str(HERE / "scanned_image_only.pdf"), pagesize=(595, 842), pageCompression=1)
    pdf.drawImage(ImageReader(image), 0, 0, width=595, height=842)
    pdf.showPage()
    pdf.save()
    image.close()


page_doc("basic_text.pdf", [
    ["DOCUMENT ALPHA: PROJECT OVERVIEW", "First page: the river flows to the sea."],
    ["DOCUMENT ALPHA: CONCLUSION", "Second page: the results are positive."],
])
page_doc("table_and_headings.pdf", [
    ["Quarterly Revenue Report", "Category       Revenue", "Hardware       12000", "Software       18000"],
    ["Summary and Findings", "Total revenue increased over the last quarter."],
])
page_doc("blank_middle_page.pdf", [
    ["First page: a distinct sentence."], [], ["Third page: closing remarks."],
])
scan_doc()
if __name__ == "__main__":
    for file in sorted(HERE.glob("*.pdf")):
        print(f"{file.name}: {file.stat().st_size} bytes")
