"""Vendor-neutral trace context and safe structured stdout logging.

No collector is required; exporter failures must not affect document processing.
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

_document = contextvars.ContextVar("pdf_document_id", default=None)
_upload = contextvars.ContextVar("pdf_upload_id", default=None)
_started = False
_trace_provider = None
_log_provider = None
_otel_log_handler = None

# Defense-in-depth: redact obvious credentials and inline PDF/image payloads.
# This is not a replacement for the application rule to never log document text.
_SECRET_PATTERNS = (
    (re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;}]+"), r"\1[REDACTED]"),
    (re.compile(r"data:(?:application/pdf|image/[a-z0-9.+-]+);base64,[A-Za-z0-9+/=]+", re.I), "[REDACTED_MEDIA]"),
)


def _safe_message(value: str) -> str:
    # Reduce the length before expensive regex passes (bounded per event).
    result = value[:4096]
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result[:2000]


class JsonFormatter(logging.Formatter):
    def format(self, record):
        current = trace.get_current_span().get_span_context()
        obj = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": os.environ.get("OTEL_SERVICE_NAME", "pdf-processor"),
            "logger": record.name,
            "event": getattr(record, "event", record.name),
            "message": _safe_message(record.getMessage()),
        }
        if _document.get(): obj["document_id"] = _document.get()
        if _upload.get(): obj["upload_id"] = _upload.get()
        if current.is_valid:
            obj["trace_id"] = format(current.trace_id, "032x")
            obj["span_id"] = format(current.span_id, "016x")
        for key in ("page", "pages_total", "model", "parser", "duration_ms", "error_code", "worker_id"):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                obj[key] = value
        # Explicitly opted-in diagnostic event; only this event bypasses the
        # normal log-message truncation. It may contain confidential document
        # text, and must not be emitted to remote OTLP logs.
        if getattr(record, "event", "") == "vision.response.invalid.raw":
            for field in ("vision_raw_model_content", "vision_raw_http_response",
                          "vision_json_decode_error", "vision_attempt", "vision_finish_reason"):
                value = getattr(record, field, None)
                if value is not None:
                    obj[field] = value
        if record.exc_info:
            # Do not serialize exceptions or tracebacks: third-party exceptions
            # can contain request payloads, credentials or PDF content.
            obj["exception_type"] = record.exc_info[0].__name__
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

class TextFormatter(logging.Formatter):
    def format(self, record):
        current = trace.get_current_span().get_span_context()
        suffix = f" document={_document.get()}" if _document.get() else ""
        if current.is_valid: suffix += f" trace={current.trace_id:032x}"
        message = f"{record.levelname} {record.name} {_safe_message(record.getMessage())}{suffix}"
        if getattr(record, "event", "") == "vision.response.invalid.raw":
            # JSON-escape full multiline contents into a single Docker log
            # entry. No length cap; opting in is intentionally explicit.
            for field in ("vision_raw_model_content", "vision_raw_http_response",
                          "vision_json_decode_error", "vision_attempt", "vision_finish_reason"):
                value = getattr(record, field, None)
                if value is not None:
                    message += " " + field + "=" + json.dumps(value, ensure_ascii=False)
        return message

def setup_logging(log_format: str = "json", level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if _otel_log_handler is not None:
        root.addHandler(_otel_log_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Uvicorn installs separate plain-text handlers; route them through the
    # same formatter so all application-container stdout is JSON by default.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


class _SafeOtelLogHandler(logging.Handler):
    """Send only the same bounded JSON payload printed to stdout via OTLP.

    Unlike attaching the SDK LoggingHandler directly to the root logger, this
    avoids exporting raw LogRecord args, exception reprs or request payloads.
    SDK exporter self-diagnostics are excluded to prevent recursive exports.
    """

    def __init__(self, provider: LoggerProvider):
        super().__init__()
        self._delegate = LoggingHandler(logger_provider=provider)
        self._formatter = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("opentelemetry.", "urllib3.")) or getattr(record, "event", "") == "vision.response.invalid.raw":
            # Raw document text must never leave through the OTLP log exporter.
            return
        try:
            payload = self._formatter.format(record)
            safe = logging.LogRecord(
                record.name, record.levelno, "", 0,
                payload, (), None,
            )
            safe.created = record.created
            safe.msecs = record.msecs
            self._delegate.emit(safe)
        except Exception:
            # Collection must never cause a PDF extraction request to fail.
            self.handleError(record)


def setup_tracing(enabled: bool, endpoint: str, service_name: str,
                  logs_endpoint: str | None = None, *, export_logs: bool = False) -> None:
    """Export traces asynchronously; export logs only when explicitly enabled.

    JSON stdout logs are harvested via the host Docker logging API. Exporting
    them a second time through OTLP is optional and disabled by default.
    """
    global _started, _trace_provider, _log_provider, _otel_log_handler
    if not enabled or _started:
        return
    if export_logs and not logs_endpoint:
        logs_endpoint = endpoint.removesuffix("/v1/traces") + "/v1/logs"
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint, timeout=3),
        max_queue_size=1024, max_export_batch_size=128,
        schedule_delay_millis=2000, export_timeout_millis=3000,
    ))
    log_provider = None
    if export_logs:
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=logs_endpoint, timeout=3),
            max_queue_size=1024, max_export_batch_size=128,
            schedule_delay_millis=2000, export_timeout_millis=3000,
        ))
        _otel_log_handler = _SafeOtelLogHandler(log_provider)
        logging.getLogger().addHandler(_otel_log_handler)
    trace.set_tracer_provider(provider)
    _trace_provider = provider
    _log_provider = log_provider
    _started = True
    atexit.register(shutdown_telemetry)


def shutdown_telemetry() -> None:
    """Flush bounded buffered telemetry when a container exits normally."""
    global _started, _trace_provider, _log_provider, _otel_log_handler
    if _otel_log_handler is not None:
        root = logging.getLogger()
        root.removeHandler(_otel_log_handler)
        _otel_log_handler = None
    for provider in (_log_provider, _trace_provider):
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass
    _log_provider = _trace_provider = None
    _started = False


def tracer():
    return trace.get_tracer("pdf-processor")


def current_ids() -> tuple[str, str]:
    current = trace.get_current_span().get_span_context()
    return (f"{current.trace_id:032x}" if current.is_valid else "", _upload.get() or "")


def inject() -> str:
    carrier = {}
    propagate.inject(carrier)
    return carrier.get("traceparent", "")


def parent_context(traceparent: str):
    # Strictly validate untrusted Redis message fields before extracting context.
    if not isinstance(traceparent, str) or not re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[0-3]", traceparent
    ):
        return None
    return propagate.extract({"traceparent": traceparent})


@contextmanager
def correlate(document_id=None, upload_id=None):
    d = _document.set(str(document_id) if document_id else None)
    u = _upload.set(str(upload_id) if upload_id else None)
    try: yield
    finally:
        _upload.reset(u)
        _document.reset(d)


@contextmanager
def span(name: str, **attributes):
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None and isinstance(value, (str, int, bool, float)):
                current.set_attribute(key, value)
        start = perf_counter()
        try:
            yield current
        except Exception as exc:
            current.set_attribute("error.type", type(exc).__name__)
            current.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
        finally:
            current.set_attribute("duration_ms", round((perf_counter() - start) * 1000, 2))
