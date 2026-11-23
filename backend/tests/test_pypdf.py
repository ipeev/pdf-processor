import shutil
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.errors import ProcessingError
from app.parsers.pypdf_parser import PyPDFParser, safe_reader


def test_two_pages(sample_basic):
    pages = PyPDFParser(10).parse(sample_basic).pages
    assert len(pages) == 2
    assert [p.page for p in pages] == [1, 2]
    assert "river flows" in pages[0].markdown
    assert "results are positive" in pages[1].markdown


def test_blank_middle_page(sample_blank):
    pages = PyPDFParser(10).parse(sample_blank).pages
    assert len(pages) == 3
    assert pages[1].markdown == ""
    assert "closing remarks" in pages[2].markdown


def test_non_pdf_filename_still_parses(sample_basic, tmp_path):
    destination = tmp_path / "renamed.bin"
    shutil.copyfile(sample_basic, destination)
    assert len(PyPDFParser(3).parse(destination).pages) == 2


def test_page_limit(sample_basic):
    with pytest.raises(ProcessingError, match="PAGE_LIMIT_EXCEEDED") as exc:
        PyPDFParser(1).parse(sample_basic)
    assert "2 pages" in exc.value.message
    assert "1 page." in exc.value.message


def test_200_page_boundary(tmp_path):
    writer = PdfWriter()
    for _ in range(200):
        writer.add_blank_page(width=72, height=72)
    within_limit = tmp_path / "200-pages.pdf"
    with within_limit.open("wb") as output:
        writer.write(output)
    assert len(safe_reader(within_limit, 200).pages) == 200

    writer.add_blank_page(width=72, height=72)
    over_limit = tmp_path / "201-pages.pdf"
    with over_limit.open("wb") as output:
        writer.write(output)
    with pytest.raises(ProcessingError, match="PAGE_LIMIT_EXCEEDED") as exc:
        safe_reader(over_limit, 200)
    assert "201 pages" in exc.value.message
    assert "200 pages" in exc.value.message


def test_scan_has_no_selectable_text(sample_scanned):
    with pytest.raises(ProcessingError, match="NO_EXTRACTABLE_TEXT"):
        PyPDFParser(5).parse(sample_scanned)


def test_unreadable_pdf(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.7\ntruncated")
    with pytest.raises(ProcessingError) as err:
        PyPDFParser(10).parse(bad)
    assert err.value.code == "INVALID_PDF"


def test_encrypted_pdf(sample_basic, tmp_path):
    from pypdf import PdfReader
    writer = PdfWriter()
    writer.append_pages_from_reader(PdfReader(sample_basic))
    writer.encrypt("secret")
    path = tmp_path / "encrypted.pdf"
    with path.open("wb") as f:
        writer.write(f)
    with pytest.raises(ProcessingError, match="PDF_ENCRYPTED"):
        PyPDFParser(10).parse(path)
