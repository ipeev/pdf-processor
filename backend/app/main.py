"""FastAPI app; Redis dependency is checked on /ready but not /health."""
from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api import router
from app.library_api import router as library_router
from app.archive_store import ArchiveStore
from app.archive_monitor import ArchiveMonitor
from app.archive_gate import guarded
from app.archive_maintenance import ArchiveMaintenance, recovery_loop
from app.index_manager import IndexManager
from app.document_repository import DocumentRepository as PermanentDocumentRepository
from app.run_repository import RunRepository
from app.persistent_pipeline import PersistentPipeline
from app.config import get_settings
from app.object_store import S3ObjectStore
from app.opensearch_client import OpenSearchClient
from app.openrouter_key_monitor import OpenRouterKeyMonitor
from app.errors import ProcessingError
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository

from app.observability import setup_logging, setup_tracing, shutdown_telemetry, span

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_format, settings.log_level)
    setup_tracing(settings.observability_enabled, settings.otel_exporter_otlp_endpoint, settings.otel_service_name, settings.otel_exporter_otlp_logs_endpoint,
                  export_logs=settings.observability_log_export_enabled)
    client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    app.state.settings = settings
    app.state.redis = client
    app.state.repository = DocumentRepository(client, result_ttl=settings.result_ttl_seconds,
                                                 cache_ttl=settings.cache_ttl_seconds)
    app.state.queue = DocumentQueue(client, settings.stream_key, settings.consumer_group, settings.consumer_name)
    app.state.object_store = S3ObjectStore(settings)
    app.state.archive_store = ArchiveStore(settings, client=app.state.object_store.client)
    app.state.openrouter_monitor = OpenRouterKeyMonitor(settings)
    app.state.opensearch = OpenSearchClient(settings.opensearch_url)
    app.state.index_manager = IndexManager(app.state.opensearch)
    app.state.documents = PermanentDocumentRepository(app.state.archive_store, app.state.index_manager)
    app.state.runs = RunRepository(app.state.archive_store, app.state.index_manager, app.state.documents)
    app.state.pipeline_archive = PersistentPipeline(settings, app.state.documents, app.state.runs, app.state.repository)
    recovery_task = None
    try:
        # Initialize separate buckets; never apply temporary lifecycle to archive.
        for attempt in range(20):
            try:
                await app.state.object_store.prepare()
                await app.state.archive_store.prepare()
                break
            except ProcessingError as exc:
                if attempt == 19 or exc.code == "ARCHIVE_LIFECYCLE_UNSAFE":
                    raise
                await asyncio.sleep(2)
        maintenance = ArchiveMaintenance(app.state.pipeline_archive, app.state.queue, app.state.object_store)
        recovery_status = {"state": "unknown", "checked_at": None, "recovered": None}
        app.state.archive_monitor = ArchiveMonitor(app.state.archive_store, app.state.index_manager, recovery_status)
        recovery_task = asyncio.create_task(recovery_loop(maintenance, telemetry=recovery_status))
        yield
    finally:
        if recovery_task is not None:
            recovery_task.cancel()
            try:
                await recovery_task
            except asyncio.CancelledError:
                pass
        await app.state.opensearch.aclose()
        await app.state.openrouter_monitor.aclose()
        await client.aclose()
        app.state.object_store.client.close()
        shutdown_telemetry()


app = FastAPI(title="Async PDF Processor", version="0.12.3", lifespan=lifespan)
app.include_router(router)
app.include_router(library_router)

@app.middleware("http")
async def trace_http(request: Request, call_next):
    # HTTP entry span; do not record URLs with document text or query values.
    from app.observability import parent_context
    from opentelemetry import context as otel_context
    ctx = parent_context(request.headers.get("traceparent", ""))
    token = otel_context.attach(ctx) if ctx is not None else None
    try:
        with span("http.request", **{"http.method": request.method, "http.route": request.url.path}):
            path = request.url.path
            p = getattr(request.app.state, 'pipeline_archive', None)
            if p and request.method == 'GET' and (path.startswith('/api/v1/runs') or path.startswith('/api/v1/library') or path.startswith('/api/v1/documents/')):
                try:
                    return await guarded(p, lambda: call_next(request))
                except ProcessingError as exc:
                    return JSONResponse(status_code=exc.http_status, content={'error': {'code': exc.code, 'message': exc.message}})
            return await call_next(request)
    finally:
        if token is not None: otel_context.detach(token)


@app.exception_handler(ProcessingError)
async def processing_error_handler(request: Request, exc: ProcessingError):
    return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})
