"""ZIPs expand into existing PDF jobs; no archive jobs or filesystem extraction."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

import httpx
import pytest
from starlette.datastructures import UploadFile

from app.errors import ProcessingError
from app.schemas import ParserChoice
from app.storage import stage_uploads
from app.main import app
from tests.fake_redis import FakeRedis
from tests.test_api_documents import install_fake_backend


def make_zip(entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def archive_upload(payload):
    return UploadFile(file=BytesIO(payload), filename="bundle.zip")


@pytest.mark.asyncio
async def test_zip_stages_nested_pdf_names_without_writing_archive_paths(settings, sample_basic):
    content = sample_basic.read_bytes()
    staged = await stage_uploads([archive_upload(make_zip([
        ("team/design.pdf", content), ("team/plan.PDF", content),
    ]))], ParserChoice.gemini, settings)
    assert len(staged) == 2
    assert [x.filename for x in staged] == ["design.pdf", "plan.PDF"]
    assert all(x.temp_path.read_bytes() == content for x in staged)
    assert len({x.id for x in staged}) == 2
    assert {x.temp_path.parent for x in staged} == {settings.upload_dir}
    assert all(x.sha256 for x in staged)
    for item in staged:
        item.temp_path.unlink()


@pytest.mark.asyncio
async def test_http_zip_uses_normal_jobs_and_cache(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    pdf = sample_basic.read_bytes()
    zip_data = make_zip([("docs/one.pdf", pdf), ("docs/two.pdf", pdf)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("many.ZIP", zip_data, "application/zip")
        })
        assert response.status_code == 202, response.text
        docs = response.json()["documents"]
        assert [d["filename"] for d in docs] == ["one.pdf", "two.pdf"]
        assert len({d["id"] for d in docs}) == 2
        assert [d["cache"] for d in docs] == ["miss", "shared"]
        assert len(fake.streams["api:jobs"]) == 1
        assert (await client.get(f"/api/v1/documents/{docs[0]['id']}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("entries,code", [
    ([("../escape.pdf", b"%PDF-1.5")], "UNSAFE_ZIP_ENTRY"),
    ([("/abs/path.pdf", b"%PDF-1.5")], "UNSAFE_ZIP_ENTRY"),
    ([("C:/Windows/a.pdf", b"%PDF-1.5")], "UNSAFE_ZIP_ENTRY"),
    ([("safe.pdf", b"%PDF-1.5"), ("note.txt", b"secret")], "ZIP_PDF_ONLY"),
    ([("archive.zip", b"nested")], "ZIP_PDF_ONLY"),
    ([("report.pdf", b"not really a PDF")], "NOT_A_PDF"),
    ([], "ZIP_NO_PDFS"),
])
async def test_zip_rejected_without_partial_staging(settings, entries, code):
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(make_zip(entries))], ParserChoice.pypdf, settings)
    assert exc.value.code == code
    assert list(settings.upload_dir.glob("*.part")) == []


@pytest.mark.asyncio
async def test_21_pdfs_rejected(settings):
    payload = make_zip([(f"file{i}.pdf", b"%PDF-1.5\n") for i in range(21)])
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(payload)], ParserChoice.pypdf, settings)
    assert exc.value.code == "ZIP_PDF_COUNT_EXCEEDED"
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_bounded_uncompressed_size(settings):
    oversized = b"%PDF-1.5\n" + b"A" * (1024 * 1024)
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(make_zip([("big.pdf", oversized)]))], ParserChoice.pypdf, settings)
    assert exc.value.code == "UPLOAD_TOO_LARGE"
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_archive_and_mixed_batch(settings):
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(b"not a ZIP")], ParserChoice.pypdf, settings)
    assert exc.value.code == "INVALID_ZIP"
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(b"archive"), UploadFile(file=BytesIO(b"%PDF-1.5"), filename="one.pdf")], ParserChoice.pypdf, settings)
    assert exc.value.code == "ZIP_MUST_BE_ALONE"


@pytest.mark.asyncio
async def test_symlink_rejected(settings):
    info = ZipInfo("link.pdf")
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    payload = make_zip([(info, b"%PDF-1.5")])
    with pytest.raises(ProcessingError) as exc:
        await stage_uploads([archive_upload(payload)], ParserChoice.pypdf, settings)
    assert exc.value.code == "UNSAFE_ZIP_ENTRY"


@pytest.mark.asyncio
async def test_zip_http_invalid_entry_does_not_enqueue_anything(settings, sample_basic):
    fake = FakeRedis()
    install_fake_backend(fake, settings)
    payload = make_zip([("ok.pdf", sample_basic.read_bytes()), ("../bad.pdf", sample_basic.read_bytes())])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/documents", data={"parser": "pypdf"}, files={
            "files": ("bundle.zip", payload, "application/zip")
        })
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSAFE_ZIP_ENTRY"
    assert "api:jobs" not in fake.streams
    assert list(settings.upload_dir.iterdir()) == []
