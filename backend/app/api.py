"""Thin HTTP layer; never parses PDF or calls OpenRouter during upload."""
import logging
import hmac
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.cache import fingerprint
from app.archive_gate import guarded
from app.cache_queue import CacheOutcome, register_upload
from app.config import Settings
from app.errors import ProcessingError, EnqueueOutcomeUnknown
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.schemas import (
    DocumentAccepted, UploadAccepted, DocumentStatus, ErrorResponse,
    ParserChoice, JobState, PageList, SafeError, SummaryRetryAccepted,
)
from app.storage import stage_uploads
from app.system_monitor import record_cache_outcome, system_snapshot
from app.docker_telemetry import DockerBatch, receive_samples
from app.observability import span, correlate

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def deps(req: Request) -> tuple[Settings, DocumentRepository, DocumentQueue]:
    return req.app.state.settings, req.app.state.repository, req.app.state.queue


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    try:
        await request.app.state.redis.ping()
        await request.app.state.object_store.ready()
        pipeline = getattr(request.app.state, "pipeline_archive", None)
        if pipeline:
            await pipeline.archive.ready()
        return {"status": "ready", "redis": "ok", "storage": "ok"}
    except ProcessingError:
        return JSONResponse(status_code=503, content={"error": {"code": "STORAGE_UNAVAILABLE", "message": "Object storage is unavailable."}})
    except RedisError:
        return JSONResponse(status_code=503, content={"error": {"code": "REDIS_UNAVAILABLE", "message": "Redis is unavailable."}})


@router.get("/system/status")
async def system_status(request: Request):
    _, _, queue = deps(request)
    snapshot = await system_snapshot(
        request.app.state.redis, request.app.state.object_store, queue,
        openrouter_monitor=getattr(request.app.state, "openrouter_monitor", None),
        opensearch=getattr(request.app.state, "opensearch", None),
        archive_monitor=getattr(request.app.state, "archive_monitor", None),
    )
    return JSONResponse(content=snapshot, headers={"Cache-Control": "no-store"})


@router.post("/system/docker-telemetry", status_code=204)
async def ingest_docker_telemetry(request: Request):
    """Authenticated local-only ingest; never accepts Docker socket/commands."""
    settings = request.app.state.settings
    expected = settings.docker_telemetry_token.get_secret_value()
    provided = request.headers.get("X-Docker-Telemetry-Token", "")
    if not settings.docker_telemetry_enabled or not expected or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Telemetry ingestion disabled or unauthorized")
    # The Compose API port is published on host loopback only; authentication
    # is still mandatory because local clients can reach that listener.
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 32768:
            raise HTTPException(status_code=413, detail="Telemetry payload too large")
        data.extend(chunk)
    try:
        batch = DockerBatch.model_validate_json(bytes(data))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid container telemetry") from exc
    await receive_samples(request.app.state.redis, batch)
    return None


@router.post("/documents", response_model=UploadAccepted, status_code=202, responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}})
async def upload_documents(request: Request, files: list[UploadFile] = File(...), parser: str = Form(...)):
    upload_id = uuid4()
    with correlate(upload_id=upload_id):
        with span("upload.accept", **{"upload.id": str(upload_id)}):
            pipeline = getattr(request.app.state, "pipeline_archive", None)
            if pipeline:
                return await guarded(pipeline, lambda: _upload_documents_inner(request, files, parser), reconcile_on_error=True)
            return await _upload_documents_inner(request, files, parser)


async def _upload_documents_inner(request: Request, files: list[UploadFile], parser: str):
    settings, repo, queue = deps(request)
    try:
        choice = ParserChoice(parser)
    except ValueError as exc:
        raise ProcessingError("UNAVAILABLE_PARSER", "Unknown parser selection.", 422) from exc
    if choice == ParserChoice.vision_figures and getattr(request.app.state, 'pipeline_archive', None) is None:
        raise ProcessingError('VISION_ARCHIVE_REQUIRED', 'AI OCR + Pictures requires permanent archive storage.', 503)
    try:
        await request.app.state.redis.ping()
    except RedisError as exc:
        raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    with span("upload.stage", **{"document.parser": choice.value, "upload.files": len(files)}):
        staged = await stage_uploads(files, choice, settings)
    store = request.app.state.object_store
    archive_pipeline = getattr(request.app.state, "pipeline_archive", None)
    accepted: list[DocumentAccepted] = []
    # Each validated upload is registered atomically. Distinct uploads always
    # receive distinct UUIDs, even when one shared job does the AI work.
    # Unregistered objects are removed if a later registration fails.
    registered: set = set()
    uncertain: set = set()
    try:
        for item in staged:
            with correlate(item.id):
                with span("document.accept", **{"document.id": str(item.id), "document.parser": choice.value}):
                    digest = fingerprint(item.sha256, choice, settings)
                    run = await archive_pipeline.register(item, digest) if archive_pipeline else None
                    with span("rustfs.put"):
                        await store.put(item.id, item.temp_path)
                    try:
                        with span("redis.enqueue"):
                            outcome = await register_upload(request.app.state.redis, queue, repo, item,
                                                            digest, cache_ttl=settings.cache_ttl_seconds, durable=archive_pipeline is not None)
                    except EnqueueOutcomeUnknown:
                        uncertain.add(item.id)
                        raise
                    log.info("Document upload registered: id=%s cache=%s", item.id, outcome.name.lower(),
                             extra={"event": "document.upload.registered"})
            registered.add(item.id)
            if archive_pipeline:
                # Admission is already known to have committed. The initial
                # manifest and queue entry are durable/registered respectively;
                # a worker can repeat the cache decision if this projection fails.
                try:
                    meta = await repo.get_meta(item.id)
                    if meta:
                        await archive_pipeline.record_registration(run, meta)
                except (ProcessingError, RedisError):
                    log.warning("Archive cache-decision projection deferred: run=%s", item.id)
            await record_cache_outcome(request.app.state.redis, outcome)
            # Hit/shared requests have no extraction job. Their lightweight
            # archive-copy jobs use the canonical archive, not temporary PDFs.
            if outcome != CacheOutcome.OWNER:
                try:
                    await store.remove(item.id)
                except ProcessingError:
                    log.warning("Duplicate source object %s awaits S3 lifecycle expiry", item.id)
            accepted.append(DocumentAccepted(
                id=item.id, document_id=item.sha256 if archive_pipeline else None, filename=item.filename, parser=choice,
                status=JobState.completed if outcome == CacheOutcome.HIT and not archive_pipeline else JobState.queued,
                cache={CacheOutcome.HIT: "hit", CacheOutcome.SHARED: "shared",
                       CacheOutcome.OWNER: "miss"}[outcome],
            ))
    except Exception:
        for item in staged:
            if item.id not in registered and item.id not in uncertain:
                try:
                    await store.remove(item.id)
                except ProcessingError:
                    log.warning("Unable to remove unqueued source object %s", item.id)
        raise
    finally:
        for item in staged:
            item.temp_path.unlink(missing_ok=True)
    return UploadAccepted(documents=accepted)


@router.get("/documents/{document_id}", response_model=DocumentStatus)
async def document_status(request: Request, document_id: UUID):
    _, repo, _ = deps(request)
    archive_pipeline = getattr(request.app.state, "pipeline_archive", None)
    if archive_pipeline:
        return await archive_pipeline.status(document_id)
    try:
        meta = await repo.get_meta(document_id)
    except RedisError as exc:
        raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    if not meta:
        raise ProcessingError("DOCUMENT_NOT_FOUND", "Document does not exist.", 404)
    effective_id = document_id
    effective = meta
    if meta.get("cache_source_id"):
        effective_id = UUID(meta["cache_source_id"])
        try:
            effective = await repo.get_meta(effective_id)
        except RedisError as exc:
            raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
        if not effective:
            raise ProcessingError("CACHE_SOURCE_EXPIRED", "Shared processing result is no longer available.", 410)
    summary = None
    if effective.get("status") == "completed":
        try:
            _, summary = await repo.read_completed(effective_id, effective)
        except RedisError as exc:
            raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    error = None
    if effective.get("status") in ("failed", "partial"):
        # Older Redis records may not have diagnostics; never expose unknown
        # fields or untrusted provider response contents.
        from app.provider_diagnostics import sanitize_diagnostic
        import json
        details = None
        if effective.get("error_diagnostic"):
            try:
                details = sanitize_diagnostic(json.loads(effective["error_diagnostic"])) or None
            except (ValueError, TypeError):
                details = None
        error = SafeError(code=effective.get("error_code") or "PROCESSING_FAILED",
                          message=effective.get("error_message") or "Processing failed.",
                          diagnostic=details)
    try:
        return DocumentStatus(
            id=document_id, filename=meta["filename"], parser=meta["parser"], status=effective["status"],
            page_count=int(effective["page_count"]) if effective.get("page_count") else None,
            page_progress=repo.parse_page_progress(effective),
            summary=summary, error=error, created_at=meta["created_at"], updated_at=effective["updated_at"],
            cache=meta.get("cache", "miss"),
            trace_id=effective.get("trace_id") or None, upload_id=UUID(meta["upload_id"]) if meta.get("upload_id") else None,
        )
    except (KeyError, ValueError) as exc:
        raise ProcessingError("RESULT_CORRUPT", "Document metadata is inconsistent.", 503) from exc


@router.get("/documents/{document_id}/pages", response_model=PageList)
async def document_pages(request: Request, document_id: UUID):
    _, repo, _ = deps(request)
    archive_pipeline = getattr(request.app.state, "pipeline_archive", None)
    if archive_pipeline:
        return await archive_pipeline.pages(document_id)
    try:
        meta = await repo.get_meta(document_id)
    except RedisError as exc:
        raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    if not meta:
        raise ProcessingError("DOCUMENT_NOT_FOUND", "Document does not exist.", 404)
    try:
        source_id = UUID(meta["cache_source_id"]) if meta.get("cache_source_id") else document_id
        parsed = await repo.read_extracted(source_id)
        pages = [{"page": p.page, "markdown": p.markdown} for p in parsed.pages]
    except RedisError as exc:
        raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    return PageList(id=document_id, pages=pages)


@router.post("/documents/{document_id}/retry-summary", response_model=SummaryRetryAccepted, status_code=202)
async def retry_document_summary(request: Request, document_id: UUID):
    _, repo, queue = deps(request)
    archive_pipeline = getattr(request.app.state, "pipeline_archive", None)
    if archive_pipeline:
        try:
            await guarded(archive_pipeline, lambda: archive_pipeline.retry_summary(document_id, queue), reconcile_on_error=True)
        except RedisError as exc:
            raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
        return SummaryRetryAccepted(id=document_id, status=JobState.summarizing)
    try:
        meta = await repo.get_meta(document_id)
        if not meta:
            raise ProcessingError("DOCUMENT_NOT_FOUND", "Document does not exist.", 404)
        source_id = UUID(meta["cache_source_id"]) if meta.get("cache_source_id") else document_id
        await queue.enqueue_summary_retry(source_id, repo)
    except RedisError as exc:
        raise ProcessingError("REDIS_UNAVAILABLE", "Redis is unavailable.", 503) from exc
    return SummaryRetryAccepted(id=document_id, status=JobState.summarizing)
