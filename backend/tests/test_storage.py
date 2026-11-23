from io import BytesIO
from uuid import UUID

import pytest
from starlette.datastructures import UploadFile

from app.errors import ProcessingError
from app.schemas import ParserChoice
from app.storage import stage_uploads, display_filename
from app.object_store import LocalTestObjectStore, object_key


def uploaded(data: bytes, filename="document.pdf") -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=filename)


@pytest.mark.asyncio
async def test_stream_to_temporary_stage_then_local_fake_store(settings, sample_basic):
    data = sample_basic.read_bytes()
    files = await stage_uploads([uploaded(data, "../../secret.pdf")], ParserChoice.pypdf, settings)
    assert len(files) == 1 and files[0].size == len(data)
    assert files[0].filename == "secret.pdf"
    assert files[0].temp_path.exists()
    await LocalTestObjectStore(settings.upload_dir).put(files[0].id, files[0].temp_path)
    assert files[0].final_path.read_bytes() == data
    assert files[0].final_path.parent == settings.upload_dir
    assert files[0].final_path.name == f"{files[0].id}.pdf"


@pytest.mark.asyncio
async def test_invalid_second_cleans_all(settings, sample_basic):
    with pytest.raises(ProcessingError, match="NOT_A_PDF"):
        await stage_uploads([uploaded(sample_basic.read_bytes()), uploaded(b"not pdf")], ParserChoice.pypdf, settings)
    assert list(settings.upload_dir.glob("*.part")) == []
    assert list(settings.upload_dir.glob("*.pdf")) == []


@pytest.mark.asyncio
async def test_size_limit_and_cleanup(settings):
    with pytest.raises(ProcessingError, match="UPLOAD_TOO_LARGE"):
        await stage_uploads([uploaded(b"%PDF-" + b"X" * (1024 * 1024 + 1))], ParserChoice.pypdf, settings)
    assert list(settings.upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_too_many_files(settings):
    small = uploaded(b"%PDF-1.7")
    with pytest.raises(ProcessingError, match="FILE_COUNT_EXCEEDED"):
        await stage_uploads([small] * (settings.max_files_per_request + 1), ParserChoice.pypdf, settings)


@pytest.mark.asyncio
async def test_empty_file(settings):
    with pytest.raises(ProcessingError, match="EMPTY_FILE"):
        await stage_uploads([uploaded(b"")], ParserChoice.pypdf, settings)


@pytest.mark.asyncio
async def test_header_with_leading_junk(settings):
    staged = await stage_uploads([uploaded(b"\x00random" + b"%PDF-1.5\n...")], ParserChoice.pypdf, settings)
    assert staged[0].size > 0


def test_uuid_object_key(settings):
    assert object_key(UUID(int=1)).endswith(".pdf")
    with pytest.raises(ProcessingError):
        object_key("../../../etc/passwd")


def test_safe_display_name():
    assert display_filename("C:\\temp\\secret\x00.pdf") == "secret.pdf"
