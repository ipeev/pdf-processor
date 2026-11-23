"""Public persistent library/history API. No raw store credentials."""
import asyncio
import io
import json
import zipfile
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api import retry_document_summary
from app.archive_gate import guarded
from app.archive_maintenance import ArchiveMaintenance
from app.catalogue_models import DocumentID, DocumentManifest, RunManifest
from app.catalogue_query import LibraryFilters, RunFilters, search_page
from app.errors import ProcessingError
from app.provider_diagnostics import sanitize_diagnostic
from app.schemas import JobState, ParserChoice, PageList, SafeError, SummaryRetryAccepted
from app.persistent_pipeline import encode_pages

router = APIRouter(prefix='/api/v1', tags=['persistent catalogue'])
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    document_id: DocumentID
    sha256: DocumentID
    display_name: str
    filename: str
    filenames: tuple[str, ...]
    file_size_bytes: int
    page_count: int | None
    preferred_run_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: UUID
    document_id: DocumentID
    upload_id: UUID | None
    filename: str
    parser: ParserChoice
    status: JobState
    cache: Literal['miss', 'hit', 'shared']
    cache_source_run_id: UUID | None
    page_count: int | None
    extraction_model: str | None
    summary_model: str | None
    pipeline_version: str
    application_version: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    worker_attempts: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    trace_id: str | None
    error: SafeError | None = None
    has_extraction: bool
    has_summary: bool
    revision: int


class RunDetail(RunView):
    summary: str | None = None


class DocumentPage(BaseModel):
    items: list[DocumentView]
    next_cursor: str | None


class RunPage(BaseModel):
    items: list[RunView]
    next_cursor: str | None


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str = Field(min_length=1, max_length=255)

    @field_validator('display_name')
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not value or value in {'.', '..'} or re.search(r'[\\/\x00-\x1f\x7f]', value):
            raise ValueError('A nonblank display name without path separators or controls is required')
        return value


class PreferredRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: UUID


def document_view(doc: DocumentManifest):
    return DocumentView.model_validate({**doc.model_dump(), 'display_name': doc.filename})


def run_view(run: RunManifest):
    error = None
    if run.status in {JobState.failed, JobState.partial}:
        error = SafeError(code=run.error_code or 'PROCESSING_FAILED', message=run.error_message or 'Processing failed.',
            diagnostic=sanitize_diagnostic(run.error_diagnostic) or None)
    return RunView.model_validate({**run.model_dump(), 'error': error})


def pipeline(request):
    value = getattr(request.app.state, 'pipeline_archive', None)
    if value is None:
        raise ProcessingError('ARCHIVE_UNAVAILABLE', 'Permanent archive is unavailable.', 503)
    return value


async def get_document(p, document_id):
    try:
        return await p.documents.get(document_id)
    except ProcessingError as exc:
        if exc.code == 'ARCHIVE_NOT_FOUND':
            raise ProcessingError('DOCUMENT_NOT_FOUND', 'Document does not exist.', 404) from exc
        raise


async def get_run(p, run_id):
    try:
        return await p.get(run_id)
    except ProcessingError as exc:
        if exc.code == 'DOCUMENT_NOT_FOUND':
            raise ProcessingError('RUN_NOT_FOUND', 'Processing run does not exist.', 404) from exc
        raise


async def committed(awaitable):
    try:
        return await awaitable
    except ProcessingError as exc:
        if exc.code == 'ARCHIVE_NOT_FOUND':
            raise ProcessingError('RESULT_CORRUPT', 'Committed archive artifact is missing.', 503) from exc
        raise


def attachment(filename, suffix):
    name = re.sub(r'[\\/\x00-\x1f\x7f]', '_', filename).strip(' .')[:180] or 'document'
    if name.lower().endswith('.pdf'):
        name = name[:-4] or 'document'
    if suffix == '.pdf' or not name.lower().endswith(suffix):
        name += suffix
    # Only an ASCII fallback and RFC 5987 encoded name enter the header.
    return {**HEADERS, 'Content-Disposition': f"attachment; filename=\"document{suffix}\"; filename*=UTF-8''{quote(name, safe='')}"}


class TemporaryDownload(FileResponse):
    def __init__(self, directory, path, **kwargs):
        self.directory = directory
        super().__init__(path, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)


@router.get('/library', response_model=DocumentPage)
async def library(request: Request, response: Response, filters: Annotated[LibraryFilters, Query()]):
    p = pipeline(request)
    await p.archive.ready()
    items, cursor = await search_page(p.documents.indexes, 'documents', filters)
    response.headers.update(HEADERS)
    return DocumentPage(items=[document_view(d) for d in items if d.revision > await p.archive.document_revision_floor(d.document_id)], next_cursor=cursor)


@router.get('/runs', response_model=RunPage)
async def history(request: Request, response: Response, filters: Annotated[RunFilters, Query()]):
    p = pipeline(request)
    await p.archive.ready()
    items, cursor = await search_page(p.runs.indexes, 'runs', filters)
    response.headers.update(HEADERS)
    return RunPage(items=[run_view(r) for r in items if not await p.archive.run_deleted(r.run_id)
                          and await p.archive.optional(f"{r.document_id}/metadata.json") is not None], next_cursor=cursor)


@router.get('/library/{document_id}', response_model=DocumentView)
async def document(request: Request, response: Response, document_id: DocumentID):
    response.headers.update(HEADERS)
    return document_view(await get_document(pipeline(request), document_id))


@router.get('/library/{document_id}/runs', response_model=RunPage)
async def document_runs(request: Request, response: Response, document_id: DocumentID, filters: Annotated[RunFilters, Query()]):
    p = pipeline(request)
    await get_document(p, document_id)
    if filters.document_id is not None and filters.document_id != document_id:
        raise ProcessingError('INVALID_DOCUMENT_FILTER', 'Document filter does not match path.', 422)
    items, cursor = await search_page(p.runs.indexes, 'runs', filters.model_copy(update={'document_id': document_id}))
    response.headers.update(HEADERS)
    return RunPage(items=[run_view(r) for r in items if not await p.archive.run_deleted(r.run_id)
                          and await p.archive.optional(f"{r.document_id}/metadata.json") is not None], next_cursor=cursor)


@router.patch('/library/{document_id}', response_model=DocumentView)
async def rename(request: Request, response: Response, document_id: DocumentID, body: RenameRequest):
    p = pipeline(request)
    async def operation():
        await get_document(p, document_id)
        return await p.documents.rename(document_id, body.display_name)
    result = await guarded(p, operation, reconcile_on_error=True)
    response.headers.update(HEADERS)
    return document_view(result)


@router.patch('/library/{document_id}/preferred-run', response_model=DocumentView)
async def preferred(request: Request, response: Response, document_id: DocumentID, body: PreferredRequest):
    p = pipeline(request)
    async def operation():
        await get_document(p, document_id)
        run = await get_run(p, body.run_id)
        if run.document_id != document_id or run.status != JobState.completed:
            raise ProcessingError('INVALID_PREFERRED_RUN', 'Preferred run must be a completed run of this document.', 409)
        return await p.documents.set_preferred(document_id, body.run_id)
    result = await guarded(p, operation, reconcile_on_error=True)
    response.headers.update(HEADERS)
    return document_view(result)


@router.get('/library/{document_id}/original', response_class=FileResponse, responses={200: {'content': {'application/pdf': {}}}})
async def original(request: Request, document_id: DocumentID):
    p = pipeline(request)
    doc = await get_document(p, document_id)
    # Stage to private disk, verify checksum before sending response headers.
    directory = tempfile.mkdtemp(prefix='pdf-download-')
    path = Path(directory) / 'original.pdf'
    task = asyncio.create_task(committed(p.archive.fetch_original(document_id, path)))
    try:
        await asyncio.shield(task)
        if path.stat().st_size != doc.file_size_bytes:
            raise ProcessingError('RESULT_CORRUPT', 'Archived original length is inconsistent.', 503)
        return TemporaryDownload(directory, path, media_type='application/pdf', headers=attachment(doc.filename, '.pdf'))
    except BaseException:
        # Shield the underlying thread; wait before removing its directory.
        try:
            await task
        except BaseException:
            pass
        shutil.rmtree(directory, ignore_errors=True)
        raise


@router.get('/runs/{run_id}', response_model=RunDetail)
async def run_detail(request: Request, response: Response, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    response.headers.update(HEADERS)
    summary = await committed(p.read_summary(run)) if run.has_summary else None
    return RunDetail(**run_view(run).model_dump(), summary=summary)


@router.get('/runs/{run_id}/pages', response_model=PageList)
async def pages(request: Request, response: Response, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    response.headers.update(HEADERS)
    return PageList(id=run_id, pages=encode_pages(await committed(p.read_extraction(run))))


@router.get('/runs/{run_id}/markdown', response_class=Response, responses={200: {'content': {'text/markdown': {}}}})
async def markdown(request: Request, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    if not run.has_extraction or not run.extraction_ref:
        raise ProcessingError('RESULT_NOT_READY', 'Extracted Markdown is not available yet.', 409)
    artifact = await committed(p.archive.read_run_artifact(run.document_id, run_id, 'document.md', generation=run.extraction_ref))
    try:
        artifact.data.decode('utf-8')
    except UnicodeError as exc:
        raise ProcessingError('RESULT_CORRUPT', 'Archived Markdown is inconsistent.', 503) from exc
    return Response(artifact.data, media_type='text/markdown', headers=attachment(run.filename, '.md'))


@router.get('/runs/{run_id}/figures')
async def figure_index(request: Request, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    if run.parser.value != 'vision_figures':
        return Response(json.dumps({'version': 1, 'figures': [], 'warnings': []}),
                        media_type='application/json', headers=HEADERS)
    return Response(json.dumps(await committed(p.read_figures(run))),
                    media_type='application/json', headers=HEADERS)


@router.get('/runs/{run_id}/images/{filename}', response_class=Response)
async def figure_image(request: Request, run_id: UUID, filename: str):
    p = pipeline(request)
    run = await get_run(p, run_id)
    data = await committed(p.read_figure(run, filename))
    return Response(data, media_type='image/png', headers={**HEADERS, 'Content-Disposition': 'inline',
                    'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.get('/runs/{run_id}/bundle.zip', response_class=FileResponse)
async def figure_bundle(request: Request, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    if run.parser.value != 'vision_figures' or not run.has_extraction:
        raise ProcessingError('RESULT_NOT_READY', 'Figure bundle is available for completed figure extraction.', 409)
    parsed = await committed(p.read_extraction(run))
    figures = await committed(p.read_figures(run, parsed))
    markdown = await committed(p.archive.read_run_artifact(run.document_id, run.run_id,
                                                          'document.md', generation=run.extraction_ref))
    directory = tempfile.mkdtemp(prefix='pdf-figure-bundle-')
    destination = Path(directory) / 'document-with-pictures.zip'
    try:
        def build():
            with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=5, allowZip64=True) as out:
                out.writestr('document.md', markdown.data)
                out.writestr('figures.json', json.dumps(figures, ensure_ascii=False).encode())
        await asyncio.to_thread(build)
        # Validate and append only server-chosen, run-owned, checksum-verified files.
        for entry in figures['figures']:
            data = await committed(p.read_figure(run, entry['filename']))
            def append(name=entry['filename'], payload=data):
                with zipfile.ZipFile(destination, 'a', compression=zipfile.ZIP_STORED, allowZip64=True) as out:
                    out.writestr('images/' + name, payload)
            await asyncio.to_thread(append)
        return TemporaryDownload(directory, destination, media_type='application/zip',
                                 headers=attachment(run.filename, '-pictures.zip'))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@router.get('/runs/{run_id}/summary', response_class=Response, responses={200: {'content': {'text/markdown': {}}}})
async def summary(request: Request, run_id: UUID):
    p = pipeline(request)
    run = await get_run(p, run_id)
    return Response(await committed(p.read_summary(run)), media_type='text/markdown', headers=attachment(run.filename, '-summary.md'))


@router.post('/runs/{run_id}/retry-summary', response_model=SummaryRetryAccepted, status_code=202)
async def retry(request: Request, response: Response, run_id: UUID):
    p = pipeline(request)
    await get_run(p, run_id)
    response.headers.update(HEADERS)
    return await retry_document_summary(request, run_id)


def maintenance(request):
    return ArchiveMaintenance(pipeline(request), request.app.state.queue, request.app.state.object_store)


@router.delete('/runs/{run_id}', status_code=204, response_class=Response)
async def delete_run(request: Request, run_id: UUID):
    try:
        await maintenance(request).request_delete('run', run_id)
    except ProcessingError as exc:
        if exc.code == 'DOCUMENT_NOT_FOUND':
            raise ProcessingError('RUN_NOT_FOUND', 'Processing run does not exist.', 404) from exc
        raise
    return Response(status_code=204, headers=HEADERS)


@router.delete('/library/{document_id}', status_code=204, response_class=Response)
async def delete_document(request: Request, document_id: DocumentID):
    # SHA-256 can be re-created; bind destructive confirmation to its revision.
    tag = request.headers.get('if-match', '')
    if not re.fullmatch(r'"[1-9][0-9]{0,18}"', tag) or int(tag[1:-1]) >= 2**63-1:
        raise ProcessingError('DELETE_PRECONDITION_REQUIRED', 'Supply the current document revision as a quoted If-Match header.', 428)
    try:
        await maintenance(request).request_delete('document', document_id, expected_revision=int(tag[1:-1]))
    except ProcessingError as exc:
        if exc.code == 'ARCHIVE_NOT_FOUND':
            raise ProcessingError('DOCUMENT_NOT_FOUND', 'Document does not exist.', 404) from exc
        raise
    return Response(status_code=204, headers=HEADERS)
