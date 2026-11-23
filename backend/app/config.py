"""Application configuration; no remote requests or mandatory LLM key at import."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True)

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Model selection is per pipeline task. Legacy GEMINI_MODEL/GPT_MODEL env
    # names remain accepted for existing installations, but new deployments
    # should use the task-oriented names below.
    native_pdf_model: str = Field(
        "inclusionai/ling-3.0-flash-vl",
        validation_alias=AliasChoices("NATIVE_PDF_MODEL", "GEMINI_MODEL"),
    )
    image_ocr_model: str = Field(
        "deepseek/deepseek-v4.1-flash-1017",
        validation_alias=AliasChoices("IMAGE_OCR_MODEL", "GPT_MODEL"),
    )
    summary_model: str = "deepseek/deepseek-v4.1-flash-1017"
    # Fourth upload parser: persistent figures and Markdown.
    vision_figures_model: str = "anthropic/claude-haiku-5.5"
    vision_figures_max_tokens_per_page: int = Field(16000, ge=1024, le=32768)
    max_vision_figures_pages_per_document: int = Field(50, ge=1, le=200)
    max_vision_figures_per_page: int = Field(8, ge=0, le=30)
    max_vision_asset_bytes: int = Field(6_000_000, ge=1024, le=20_000_000)
    max_vision_total_asset_bytes: int = Field(100_000_000, ge=1024, le=500_000_000)
    vision_figure_padding_norm: int = Field(20, ge=0, le=40)
    log_format: str = Field("json", pattern="^(json|text)$")
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
    observability_enabled: bool = True
    observability_log_export_enabled: bool = False
    otel_service_name: str = "pdf-processor"
    otel_exporter_otlp_endpoint: str = "http://host.docker.internal:4318/v1/traces"
    otel_exporter_otlp_logs_endpoint: str = "http://host.docker.internal:4318/v1/logs"
    worker_threads: int = Field(20, ge=1, le=100)
    redis_url: str = "redis://redis:6379/0"
    opensearch_url: str = "http://opensearch:9200"
    # Local upload staging is private to each container; original PDFs live in S3.
    upload_dir: Path = Path("/tmp/pdf-uploads")
    s3_endpoint_url: str = "http://rustfs:9000"
    s3_bucket: str = "pdf-uploads"
    s3_archive_bucket: str = "pdf-documents"
    s3_access_key: SecretStr = SecretStr("pdfprocessor")
    s3_secret_key: SecretStr = SecretStr("local-demo-secret-change-me")
    s3_lifecycle_days: int = Field(1, ge=1, le=365)
    stream_key: str = "documents:jobs"
    consumer_group: str = "pdf-workers"
    consumer_name: str = "worker"
    max_files_per_request: int = Field(5, gt=0, le=100)
    max_file_size_mib: int = Field(20, gt=0, le=1024)
    max_total_upload_mib: int = Field(50, gt=0, le=4096)
    max_pages_per_document: int = Field(200, gt=0, le=10000)
    max_ai_pages_per_document: int = Field(200, gt=0, le=10000)
    max_summary_input_chars: int = Field(400000, gt=0)
    max_ai_output_tokens_per_page: int = Field(32768, gt=0, le=100000)
    max_summary_output_tokens: int = Field(4096, gt=0, le=100000)
    openrouter_monitor_enabled: bool = True
    openrouter_monitor_refresh_seconds: int = Field(60, ge=30, le=3600)
    openrouter_timeout_seconds: int = Field(120, gt=0, le=1200)
    openrouter_max_call_attempts: int = Field(3, gt=0, le=10)
    max_worker_attempts: int = Field(3, gt=0, le=30)
    max_render_pixels: int = Field(12000000, gt=0)
    max_render_image_bytes: int = Field(6000000, gt=0)
    render_scale: float = Field(1.5, gt=0, le=4)
    worker_poll_ms: int = Field(1000, ge=100, le=60000)
    worker_heartbeat_seconds: int = Field(5, ge=1, le=60)
    worker_claim_idle_seconds: int = Field(45, ge=5, le=3600)
    result_ttl_seconds: int = Field(86400, ge=60, le=604800)
    cache_ttl_seconds: int = Field(86400, ge=60, le=604800)
    docker_telemetry_enabled: bool = False
    docker_telemetry_token: SecretStr = SecretStr("")
    run_live_tests: bool = False

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must be redis:// or rediss://")
        return value

    @field_validator("openrouter_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.startswith("https://") and not value.startswith("http://localhost"):
            raise ValueError("OpenRouter must use HTTPS, except local HTTP test server")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_caps(self):
        if not self.s3_archive_bucket or self.s3_archive_bucket == self.s3_bucket:
            raise ValueError("S3_ARCHIVE_BUCKET must be different from S3_BUCKET")
        if self.max_total_upload_mib < self.max_file_size_mib:
            raise ValueError("MAX_TOTAL_UPLOAD_MIB cannot be below MAX_FILE_SIZE_MIB")
        if self.max_ai_pages_per_document > self.max_pages_per_document:
            raise ValueError("MAX_AI_PAGES_PER_DOCUMENT cannot exceed MAX_PAGES_PER_DOCUMENT")
        if self.worker_claim_idle_seconds < self.worker_heartbeat_seconds * 3:
            raise ValueError("WORKER_CLAIM_IDLE_SECONDS must be at least 3x WORKER_HEARTBEAT_SECONDS")
        return self

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_size_mib * 1024 * 1024

    @property
    def max_total_bytes(self) -> int:
        return self.max_total_upload_mib * 1024 * 1024


@lru_cache

def get_settings() -> Settings:
    return Settings()
