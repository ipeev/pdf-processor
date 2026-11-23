import io
import json
import logging

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import TracerProvider

from app.observability import JsonFormatter, correlate, inject, parent_context, span
from app.config import Settings


def test_json_logs_are_safe_and_correlated():
    with correlate("document-1", "upload-1"):
        record = logging.LogRecord("worker", logging.ERROR, "", 4, "Page failed", (), None)
        body = json.loads(JsonFormatter().format(record))
    assert body["level"] == "ERROR"
    assert body["document_id"] == "document-1"
    assert body["upload_id"] == "upload-1"
    assert "trace_id" not in body
    record = logging.LogRecord("worker", logging.ERROR, "", 4, "Something failed", (), None)
    assert "document_id" not in json.loads(JsonFormatter().format(record))


def test_invalid_parent_context_is_rejected():
    assert parent_context("nonsense") is None
    assert parent_context("00-" + "0"*32 + "-" + "0"*16 + "-01") is not None


def test_config_defaults_and_models():
    settings = Settings(_env_file=None)
    assert settings.log_format == "json"
    assert settings.observability_enabled
    assert not settings.observability_log_export_enabled
    assert settings.native_pdf_model == "inclusionai/ling-3.0-flash-vl"
    assert settings.image_ocr_model == settings.summary_model == "deepseek/deepseek-v4.1-flash-1017"


def test_traceparent_propagates_between_queue_and_worker():
    provider = TracerProvider()
    current = provider.get_tracer("test")
    with current.start_as_current_span("parent") as parent:
        value = inject()
        assert value.startswith("00-") and len(value) == 55
        with correlate("document-2"):
            record = logging.LogRecord("worker", logging.INFO, "", 8, "job", (), None)
            result = json.loads(JsonFormatter().format(record))
            assert result["trace_id"] == f"{parent.get_span_context().trace_id:032x}"
            assert result["document_id"] == "document-2"
    ctx = parent_context(value)
    token = attach(ctx)
    try:
        assert trace.get_current_span().get_span_context().trace_id == parent.get_span_context().trace_id
    finally:
        detach(token)


def test_default_observability_is_generic_otlp_http():
    settings = Settings(_env_file=None)
    assert settings.otel_exporter_otlp_endpoint == "http://host.docker.internal:4318/v1/traces"
    assert settings.otel_exporter_otlp_logs_endpoint == "http://host.docker.internal:4318/v1/logs"


def test_otlp_forwarder_uses_safe_bounded_json_only():
    from app.observability import _SafeOtelLogHandler
    class FakeSDKHandler:
        def __init__(self): self.records=[]
        def emit(self, record): self.records.append(record)
    handler = _SafeOtelLogHandler.__new__(_SafeOtelLogHandler)
    logging.Handler.__init__(handler)
    handler._delegate = FakeSDKHandler()
    handler._formatter = JsonFormatter()
    source = logging.LogRecord("pdf.worker", logging.INFO, "", 1, "hello %s", ("world",), None)
    with correlate("doc-12", "upload-7"):
        handler.emit(source)
    assert len(handler._delegate.records) == 1
    sent = handler._delegate.records[0]
    body = json.loads(sent.getMessage())
    assert body["message"] == "hello world"
    assert body["document_id"] == "doc-12"
    assert body["upload_id"] == "upload-7"
    assert sent.args == ()
    # SDK diagnostics cannot be recursively re-exported.
    source.name = "opentelemetry.sdk.trace.export"
    handler.emit(source)
    assert len(handler._delegate.records) == 1


def test_otlp_logs_redact_common_credentials_and_media():
    record = logging.LogRecord("api", logging.WARNING, "", 1,
        "Bearer SECRET123 sk-or-v1-abcd9876 data:image/png;base64,AAAAAA==", (), None)
    message = json.loads(JsonFormatter().format(record))["message"]
    assert "SECRET123" not in message
    assert "sk-or-v1-abcd9876" not in message
    assert "AAAAAA==" not in message


def test_log_export_is_opt_in_by_default():
    import inspect
    from app.observability import setup_tracing
    signature = inspect.signature(setup_tracing)
    assert signature.parameters["export_logs"].default is False
    assert signature.parameters["export_logs"].kind is inspect.Parameter.KEYWORD_ONLY
