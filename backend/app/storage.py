"""Bounded multipart upload staging; source paths are UUID-derived only."""
from __future__ import annotations
import asyncio
import hashlib
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from fastapi import UploadFile

from app.config import Settings
from app.errors import ProcessingError
from app.schemas import ParserChoice

_CHUNK = 1024 * 1024
_ZIP_PDF_LIMIT = 20
_ZIP_ENTRY_LIMIT = 80  # includes directories; avoids excessive central-directory work


@dataclass(frozen=True)
class StagedFile:
    id: UUID
    filename: str
    parser: ParserChoice
    temp_path: Path
    final_path: Path
    size: int
    sha256: str = ""


def display_filename(raw: str | None) -> str:
    value = (raw or "document.pdf").replace("\\", "/").split("/")[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:200]
    return value or "document.pdf"


def _stage_zip(uploaded: UploadFile, parser: ParserChoice, settings: Settings) -> list[StagedFile]:
    """Read one ZIP from Starlette's spooled upload file, never unpacking paths.

    Runs in asyncio.to_thread: bounded sequential decompression, UUID-named
    temporary files, and no new queue/worker type. Nothing is enqueued until
    the *entire* archive has passed validation.
    """
    staged: list[StagedFile] = []
    current_temp: Path | None = None
    total = 0
    try:
        uploaded.file.seek(0, os.SEEK_END)
        archive_size = uploaded.file.tell()
        if archive_size > settings.max_total_bytes:
            raise ProcessingError("UPLOAD_TOO_LARGE", "ZIP file exceeds the total upload size limit.", 413)
        uploaded.file.seek(0)
        try:
            with zipfile.ZipFile(uploaded.file, "r", allowZip64=True) as archive:
                members = archive.infolist()
                if len(members) > _ZIP_ENTRY_LIMIT:
                    raise ProcessingError("ZIP_TOO_MANY_ENTRIES", "ZIP contains too many entries.", 413)
                pdf_count = 0
                for entry in members:
                    name = entry.filename.replace("\\", "/")
                    parts = PurePosixPath(name).parts
                    # Reject any path traversal, absolute/drive/UNC path, and
                    # Unix symlinks even though we never write archive names.
                    if (not name or name.startswith("/") or entry.filename.startswith("\\") or
                            ".." in parts or not parts or ":" in parts[0] or
                            ((entry.external_attr >> 16) & 0o170000) == 0o120000):
                        raise ProcessingError("UNSAFE_ZIP_ENTRY", "ZIP contains an unsafe entry path or link.", 415)
                    if entry.is_dir():
                        continue
                    if not name.lower().endswith(".pdf"):
                        raise ProcessingError("ZIP_PDF_ONLY", "ZIP archives must contain only PDF files (no nested archives).", 415)
                    if entry.flag_bits & 0x1:
                        raise ProcessingError("ZIP_ENCRYPTED", "Password-protected ZIP entries are not supported.", 415)
                    pdf_count += 1
                    if pdf_count > _ZIP_PDF_LIMIT:
                        raise ProcessingError("ZIP_PDF_COUNT_EXCEEDED", "ZIP contains more than 20 PDFs.", 413)
                    if entry.file_size > settings.max_file_bytes or total + entry.file_size > settings.max_total_bytes:
                        raise ProcessingError("UPLOAD_TOO_LARGE", "ZIP extracted PDFs exceed configured size limits.", 413)
                    doc_id = uuid4()
                    fd, temp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=settings.upload_dir)
                    current_temp = Path(temp_name)
                    size = 0
                    header = bytearray()
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "wb") as output, archive.open(entry, "r") as source:
                        while True:
                            chunk = source.read(_CHUNK)
                            if not chunk:
                                break
                            size += len(chunk)
                            total += len(chunk)
                            if size > settings.max_file_bytes or total > settings.max_total_bytes:
                                raise ProcessingError("UPLOAD_TOO_LARGE", "ZIP extracted PDFs exceed configured size limits.", 413)
                            if len(header) < 1024:
                                header.extend(chunk[:1024 - len(header)])
                            digest.update(chunk)
                            output.write(chunk)
                    if not size:
                        raise ProcessingError("EMPTY_FILE", "ZIP contains an empty PDF.", 415)
                    if b"%PDF-" not in header:
                        raise ProcessingError("NOT_A_PDF", "ZIP contains a file without a PDF signature.", 415)
                    staged.append(StagedFile(doc_id, display_filename(entry.filename), parser,
                                             current_temp, settings.upload_dir / f"{doc_id}.pdf", size, digest.hexdigest()))
                    current_temp = None
                if not pdf_count:
                    raise ProcessingError("ZIP_NO_PDFS", "ZIP must contain at least one PDF.", 415)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
            raise ProcessingError("INVALID_ZIP", "ZIP archive is invalid, damaged or uses unsupported compression.", 415) from exc
        return staged
    except BaseException:
        for item in staged:
            item.temp_path.unlink(missing_ok=True)
        if current_temp is not None:
            current_temp.unlink(missing_ok=True)
        raise


async def stage_uploads(
    files: list[UploadFile], parser: ParserChoice, settings: Settings,
) -> list[StagedFile]:
    if not files:
        raise ProcessingError("NO_FILES", "At least one PDF file or one ZIP archive is required.", 422)
    zip_files = [file for file in files if (file.filename or "").lower().endswith(".zip")]
    if zip_files:
        if len(files) != 1:
            raise ProcessingError("ZIP_MUST_BE_ALONE", "Upload a ZIP archive on its own, without other files.", 422)
        try:
            await asyncio.to_thread(settings.upload_dir.mkdir, parents=True, exist_ok=True)
            return await asyncio.to_thread(_stage_zip, zip_files[0], parser, settings)
        finally:
            await zip_files[0].close()
    if len(files) > settings.max_files_per_request:
        raise ProcessingError("FILE_COUNT_EXCEEDED", "Too many files in one upload.", 413)
    directory = settings.upload_dir
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    staged: list[StagedFile] = []
    total = 0
    current_temp: Path | None = None
    try:
        for uploaded in files:
            if not (uploaded.filename or "").lower().endswith(".pdf"):
                raise ProcessingError("NOT_A_PDF", "Only PDF files or one ZIP archive of PDFs are supported.", 415)
            doc_id = uuid4()
            fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=directory)
            current_temp = Path(tmp_name)
            size = 0
            header = bytearray()
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = await uploaded.read(_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    total += len(chunk)
                    if size > settings.max_file_bytes or total > settings.max_total_bytes:
                        raise ProcessingError("UPLOAD_TOO_LARGE", "PDF upload exceeds configured size limit.", 413)
                    if len(header) < 1024:
                        header.extend(chunk[:1024 - len(header)])
                    await asyncio.to_thread(output.write, chunk)
            if size == 0:
                raise ProcessingError("EMPTY_FILE", "An uploaded file is empty.", 415)
            if b"%PDF-" not in header:
                raise ProcessingError("NOT_A_PDF", "Uploaded content does not have a PDF signature.", 415)
            staged.append(
                StagedFile(doc_id, display_filename(uploaded.filename), parser,
                           current_temp, directory / f"{doc_id}.pdf", size, digest.hexdigest())
            )
            current_temp = None
        return staged
    except BaseException:
        for item in staged:
            item.temp_path.unlink(missing_ok=True)
        if current_temp is not None:
            current_temp.unlink(missing_ok=True)
        raise
    finally:
        for file in files:
            await file.close()
