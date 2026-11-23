"""Redis Streams worker with unique identity, live heartbeats and takeover."""
from __future__ import annotations
import asyncio
import logging
import hashlib
import os
import signal
import socket
import tempfile
from pathlib import Path
from contextlib import suppress
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.document_processor import DocumentProcessor
from app.errors import ProcessingError
from app.openrouter_client import OpenRouterClient
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository
from app.object_store import LocalTestObjectStore, S3ObjectStore
from app.archive_store import ArchiveStore
from app.opensearch_client import OpenSearchClient
from app.index_manager import IndexManager
from app.document_repository import DocumentRepository as PermanentDocumentRepository
from app.run_repository import RunRepository
from app.persistent_pipeline import PersistentPipeline
from app.system_monitor import publish_worker_state, remove_worker_state

from app.observability import setup_logging, setup_tracing, shutdown_telemetry, span, correlate, parent_context
from opentelemetry import context as otel_context
setup_logging()
log = logging.getLogger(__name__)


class DocumentWorker:
    def __init__(self, settings: Settings, queue: DocumentQueue, repo: DocumentRepository,
                 processor: DocumentProcessor, object_store=None, archive_pipeline=None):
        self.settings, self.queue, self.repo, self.processor = settings, queue, repo, processor
        # Production always passes S3ObjectStore explicitly. The local implementation
        # preserves deterministic no-S3 unit tests for the existing processing logic.
        self.object_store = object_store if object_store is not None else LocalTestObjectStore(settings.upload_dir)
        self.archive_pipeline = archive_pipeline
        self.stopping = False
        self.monitor_container = hashlib.blake2s(socket.gethostname().encode(), digest_size=4).hexdigest()
        self.monitor_doc: UUID | None = None
        self.monitor_parser: str | None = None
        self.monitor_stage = "idle"

    async def _publish_monitor(self) -> None:
        stage = self.monitor_stage
        if self.monitor_doc is not None:
            try:
                meta = await self.repo.get_meta(self.monitor_doc)
                if meta and meta.get("status") == "summarizing":
                    stage = "summarizing"
            except Exception:
                pass
        await publish_worker_state(self.queue.redis, self.queue.consumer, stage,
                                   self.monitor_parser, self.settings.worker_heartbeat_seconds,
                                   container=self.monitor_container)

    async def _monitor_loop(self) -> None:
        while not self.stopping:
            await self._publish_monitor()
            await asyncio.sleep(min(3, self.settings.worker_heartbeat_seconds))

    async def _record_error(self, doc_id: UUID, error: ProcessingError, token: str) -> None:
        # A summary failure must not erase pages already committed to Redis.
        # Log only explicitly allowlisted counters/identifiers, never provider
        # response bodies, prompts, documents, authorization or generated text.
        from app.provider_diagnostics import sanitize_diagnostic
        log.error("Document %s processing failure: code=%s diagnostic=%s",
                  doc_id, error.code, sanitize_diagnostic(error.diagnostic),
                  extra={"event": "document.processing.failed", "error_code": error.code})
        saved = await self.repo.get_saved_extraction(doc_id)
        if saved is not None:
            await self.repo.store_partial(doc_id, error, owner_token=token)
        else:
            await self.repo.store_failed(doc_id, error, owner_token=token)

    async def _keepalive(self, message_id: str, doc_id: UUID) -> None:
        """Refresh PEL idle time only while this consumer still owns the job."""
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            try:
                if not await self.queue.heartbeat(message_id):
                    log.warning("Worker %s lost stream ownership of message %s",
                                self.queue.consumer, message_id)
                    return
                await self.repo.refresh_cache_reservation(doc_id)
            except RedisError:
                # A transient Redis outage can recover; the fenced result
                # transaction will still reject writes if ownership was lost.
                log.warning("Redis heartbeat temporarily unavailable: %s", message_id)

    async def handle_message(self, message_id: str, fields: dict) -> None:
        if self.archive_pipeline:
            from app.archive_gate import guarded
            return await guarded(self.archive_pipeline, lambda: self._handle_traced_message(message_id, fields))
        return await self._handle_traced_message(message_id, fields)

    async def _handle_traced_message(self, message_id: str, fields: dict) -> None:
        """Attach the Redis-carried parent in this worker thread/event loop."""
        ctx = parent_context(fields.get("traceparent", ""))
        token = otel_context.attach(ctx) if ctx is not None else None
        try:
            with correlate(fields.get("document_id"), fields.get("upload_id")):
                with span("document.process", **{"document.id": str(fields.get("document_id", "")),
                                               "document.parser": str(fields.get("parser", "")),
                                               "worker.consumer": self.queue.consumer}):
                    await self._handle_message_inner(message_id, fields)
        finally:
            if token is not None: otel_context.detach(token)

    async def _handle_message_inner(self, message_id: str, fields: dict) -> None:
        try:
            doc_id = UUID(fields["document_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProcessingError("INVALID_JOB", "Stream entry has an invalid document ID.") from exc
        task = fields.get("action", "extract")
        if task not in {"extract", "summary", "cache_hit", "shared"}:
            raise ProcessingError("INVALID_JOB", "Stream entry has an invalid action.")
        token = uuid4().hex
        if not await self.queue.begin(message_id, doc_id, token, self.repo):
            raise ProcessingError("JOB_LEASE_LOST", "Another worker owns this job.")
        log.info("Worker %s accepted job %s action=%s message=%s", self.queue.consumer,
                 doc_id, task, message_id, extra={"event": "document.worker.accepted", "worker_id": self.queue.consumer})
        self.monitor_doc = doc_id
        self.monitor_parser = fields.get("parser")
        self.monitor_stage = "summarizing" if task == "summary" else "extracting"
        await self._publish_monitor()
        keepalive = asyncio.create_task(self._keepalive(message_id, doc_id))
        try:
            meta = await self.repo.get_meta(doc_id)
            if not meta:
                raise ProcessingError("JOB_ORPHANED", "Job metadata is missing.")
            if self.archive_pipeline and meta.get("archive_document_id"):
                if fields.get("parser") != meta.get("parser"):
                    raise ProcessingError("INVALID_JOB", "Parser differs from accepted job.")
                await self.archive_pipeline.process(doc_id, token, task, self.processor)
                await self._ack_and_cleanup(message_id, doc_id, token, task)
                return
            if task in {"cache_hit", "shared"}:
                raise ProcessingError("INVALID_JOB", "Archive job is missing durable metadata.")
            if meta.get("status") == "failed":
                await self._ack_and_cleanup(message_id, doc_id, token, task)
                return
            if meta.get("status") == "completed":
                if not await self.repo.is_complete(doc_id, meta):
                    raise ProcessingError("RESULT_CORRUPT", "Completed result is inconsistent.")
                await self._ack_and_cleanup(message_id, doc_id, token, task)
                return
            if task == "extract" and meta.get("status") == "partial":
                await self._ack_and_cleanup(message_id, doc_id, token, task)
                return
            if fields.get("parser") != meta.get("parser"):
                await self._record_error(doc_id, ProcessingError(
                    "INVALID_JOB", "Parser differs from accepted job."), token)
                await self._ack_and_cleanup(message_id, doc_id, token, task)
                return
            if task == "summary":
                if meta.get("status") not in {"summarizing", "partial"}:
                    raise ProcessingError("INVALID_JOB", "Summary retry is not in a valid state.")
                attempts = await self.repo.mark_summarizing(doc_id, owner_token=token)
                if attempts > self.settings.max_worker_attempts:
                    await self._record_error(doc_id, ProcessingError(
                        "WORKER_RETRIES_EXCEEDED", "Summary retry attempts exceeded."), token)
                    await self._ack_and_cleanup(message_id, doc_id, token, task)
                    return
            else:
                attempts = await self.repo.mark_processing(doc_id, owner_token=token)
                if attempts > self.settings.max_worker_attempts:
                    await self._record_error(doc_id, ProcessingError(
                        "WORKER_RETRIES_EXCEEDED", "Processing attempts exceeded."), token)
                    await self._ack_and_cleanup(message_id, doc_id, token, task)
                    return
            try:
                if task == "summary":
                    await self.processor.summarize_saved(doc_id, owner_token=token)
                else:
                    # Never use a shared filesystem. This private file is removed
                    # even if extraction fails or this worker loses ownership.
                    with tempfile.TemporaryDirectory(prefix="pdf-worker-") as directory:
                        local_pdf = Path(directory) / "source.pdf"
                        if await self.repo.get_saved_extraction(doc_id) is None:
                            with span("rustfs.fetch"):
                                await self.object_store.fetch(doc_id, local_pdf)
                        await self.processor.process(doc_id, fields["parser"], local_pdf,
                                                     owner_token=token)
            except ProcessingError as exc:
                if exc.code in {"REDIS_TRANSACTION_UNCERTAIN", "JOB_LEASE_LOST", "STORAGE_UNAVAILABLE"}:
                    raise
                await self._record_error(doc_id, exc, token)
            except (RedisError, ConnectionError):
                raise
            except Exception:
                log.exception("Unhandled document processing error for %s", doc_id)
                await self._record_error(doc_id, ProcessingError(
                    "PROCESSING_FAILED", "Unexpected document processing error."), token)
            await self._ack_and_cleanup(message_id, doc_id, token, task)
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive
            self.monitor_doc = None
            self.monitor_parser = None
            self.monitor_stage = "idle"
            await self._publish_monitor()

    async def _ack_and_cleanup(self, message_id: str, doc_id: UUID, token: str,
                               task: str) -> None:
        if not await self.queue.ack_owned(message_id, doc_id, token, self.repo):
            raise ProcessingError("JOB_LEASE_LOST", "Job ownership changed before acknowledgment.")
        if task == "extract":
            try:
                await self.object_store.remove(doc_id)
            except ProcessingError:
                # ACK is committed. Never replay a completed job just to delete
                # an object. The bucket lifecycle rule removes orphaned sources.
                log.warning("PDF object cleanup failed after ACK: document=%s", doc_id)
        log.info("Worker %s acknowledged document %s", self.queue.consumer, doc_id,
                 extra={"event": "document.worker.finished", "worker_id": self.queue.consumer})

    def request_shutdown(self) -> None:
        log.info("Worker shutdown requested; finishing current job if possible")
        self.stopping = True

    async def run_forever(self) -> None:
        await self.queue.ensure_group()
        monitor = asyncio.create_task(self._monitor_loop())
        try:
            await self._run_jobs()
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            await remove_worker_state(self.queue.redis, self.queue.consumer)

    async def _run_jobs(self) -> None:
        replay_cursor = "0"
        replay = True
        while not self.stopping:
            try:
                if replay:
                    entries = await self.queue.read(replay_cursor, self.settings.worker_poll_ms)
                    if not entries:
                        replay = False
                        continue
                else:
                    entries = await self.queue.claim_abandoned(
                        self.settings.worker_claim_idle_seconds * 1000)
                    if entries:
                        log.warning("Worker %s claimed abandoned message %s",
                                    self.queue.consumer, entries[0][0])
                    else:
                        entries = await self.queue.read(">", self.settings.worker_poll_ms)
                for message_id, fields in entries:
                    try:
                        await self.handle_message(message_id, fields)
                    except ProcessingError as exc:
                        if exc.code == "REDIS_TRANSACTION_UNCERTAIN":
                            raise
                        log.error("Job message %s deferred, code=%s", message_id, exc.code)
                    if replay:
                        replay_cursor = message_id
            except Exception as exc:
                if self.stopping:
                    return
                log.error("Worker %s retrying after error: %s", self.queue.consumer,
                          type(exc).__name__)
                replay_cursor = "0"
                replay = True
                await asyncio.sleep(2)
                try:
                    await self.queue.ensure_group()
                except Exception:
                    pass


async def run_worker_instance(settings: Settings, stop_event, register) -> None:
    """One complete existing worker; all async clients are created in its own thread."""
    client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=5)
    llm = OpenRouterClient(settings)
    repo = DocumentRepository(client, result_ttl=settings.result_ttl_seconds,
                              cache_ttl=settings.cache_ttl_seconds)
    consumer = f"{settings.consumer_name}-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    queue = DocumentQueue(client, settings.stream_key, settings.consumer_group, consumer)
    processor = DocumentProcessor(settings, repo, llm=llm)
    store = S3ObjectStore(settings)
    archive = ArchiveStore(settings, client=store.client)
    search = OpenSearchClient(settings.opensearch_url)
    documents = PermanentDocumentRepository(archive, IndexManager(search))
    runs = RunRepository(archive, documents.indexes, documents)
    pipeline = PersistentPipeline(settings, documents, runs, repo)
    worker = DocumentWorker(settings, queue, repo, processor, object_store=store, archive_pipeline=pipeline)
    register(worker)
    try:
        log.info("Worker starting; consumer=%s stream=%s", consumer, settings.stream_key)
        while not worker.stopping and not stop_event.is_set():
            try:
                await store.ready()
                await archive.ready()
                break
            except ProcessingError:
                log.warning("Worker waiting for S3-compatible storage")
                await asyncio.sleep(2)
        if not worker.stopping and not stop_event.is_set():
            await worker.run_forever()
    finally:
        await client.aclose()
        await llm.aclose()
        await search.aclose()
        store.client.close()


class WorkerSupervisor:
    """Run independent, single-document async workers in bounded OS threads.

    Existing Redis consumer/ownership logic stays inside each worker instance;
    only the main thread handles OS signals and process-level supervision.
    """

    def __init__(self, settings: Settings, runner=run_worker_instance):
        import threading
        self.settings, self.runner = settings, runner
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._running: dict[int, tuple[asyncio.AbstractEventLoop, DocumentWorker]] = {}
        self._last_started: dict[int, float] = {}

    def _attach(self, index: int, worker: DocumentWorker) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            self._running[index] = (loop, worker)
        if self.stopping.is_set():
            loop.call_soon(worker.request_shutdown)

    def _thread_entry(self, index: int) -> None:
        try:
            asyncio.run(self.runner(self.settings, self.stopping,
                                    lambda worker: self._attach(index, worker)))
        except Exception:
            log.exception("Worker thread %s stopped unexpectedly", index + 1)
        finally:
            with self._lock:
                self._running.pop(index, None)

    def _start(self, index: int) -> None:
        import threading
        from time import monotonic
        thread = threading.Thread(target=self._thread_entry, args=(index,),
                                  name=f"pdf-worker-{index + 1}", daemon=True)
        self._threads[index] = thread
        self._last_started[index] = monotonic()
        thread.start()

    def request_shutdown(self, *_args) -> None:
        self.stopping.set()
        with self._lock:
            instances = list(self._running.values())
        for loop, worker in instances:
            try:
                loop.call_soon_threadsafe(worker.request_shutdown)
            except RuntimeError:
                # The instance's loop already exited.
                pass

    def run(self) -> None:
        from time import monotonic
        # Register OS signals in the main thread, never in worker threads.
        signals = (signal.SIGINT, signal.SIGTERM)
        original_handlers = {sig: signal.getsignal(sig) for sig in signals}
        try:
            for stop_signal in signals:
                signal.signal(stop_signal, self.request_shutdown)
            log.info("Starting %d independent worker threads", self.settings.worker_threads)
            for index in range(self.settings.worker_threads):
                self._start(index)
            while not self.stopping.wait(0.5):
                for index, thread in tuple(self._threads.items()):
                    if not thread.is_alive() and monotonic() - self._last_started[index] >= 2.0:
                        thread.join()
                        log.warning("Restarting stopped worker instance %s", index + 1)
                        self._start(index)
        finally:
            self.request_shutdown()
            # Give in-flight jobs time to complete. Unfinished messages stay
            # pending in Redis for takeover after this process terminates.
            deadline = monotonic() + 170
            for thread in self._threads.values():
                thread.join(timeout=max(0, deadline - monotonic()))
            remaining = sum(thread.is_alive() for thread in self._threads.values())
            if remaining:
                log.warning("%d worker threads still running at shutdown deadline", remaining)
            for stop_signal, previous in original_handlers.items():
                signal.signal(stop_signal, previous)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_format, settings.log_level)
    setup_tracing(settings.observability_enabled, settings.otel_exporter_otlp_endpoint, settings.otel_service_name, settings.otel_exporter_otlp_logs_endpoint,
                  export_logs=settings.observability_log_export_enabled)
    try:
        WorkerSupervisor(settings).run()
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
