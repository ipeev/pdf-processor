"""Versioned RustFS manifests; never store result bodies in the catalogue."""
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas import JobState, ParserChoice

DocumentID = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Filename = Annotated[str, Field(min_length=1, max_length=1024)]
Revision = Annotated[int, Field(ge=1, le=2**63-1, strict=True)]


def now() -> datetime:
    return datetime.now(UTC)


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    revision: Revision = 1
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @field_validator('created_at', 'updated_at')
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Timezone required')
        return value.astimezone(UTC)

    @model_validator(mode='after')
    def dates(self):
        if self.updated_at < self.created_at:
            raise ValueError('updated_at precedes created_at')
        return self


class DocumentManifest(Manifest):
    document_id: DocumentID
    sha256: DocumentID
    filename: Filename
    filenames: tuple[Filename, ...] = Field(min_length=1)
    file_size_bytes: int = Field(gt=0, strict=True)
    page_count: int | None = Field(default=None, ge=1, strict=True)
    preferred_run_id: UUID | None = None
    s3_bucket: str = Field(min_length=1)
    s3_object_key: str

    @model_validator(mode='after')
    def identity(self):
        if (self.sha256 != self.document_id or self.s3_object_key != f'{self.document_id}/original.pdf'
                or self.filename not in self.filenames or len(set(self.filenames)) != len(self.filenames)):
            raise ValueError('Inconsistent document identity or filenames')
        return self


class RunManifest(Manifest):
    run_id: UUID
    document_id: DocumentID
    upload_id: UUID | None = None
    filename: Filename
    parser: ParserChoice
    extraction_model: str | None = None
    summary_model: str | None = None
    pipeline_version: str
    application_version: str
    processing_fingerprint: str | None = None
    status: JobState = JobState.queued
    cache: Literal['miss', 'hit', 'shared'] = 'miss'
    cache_source_run_id: UUID | None = None
    page_count: int | None = Field(default=None, ge=1, strict=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    worker_attempts: int = Field(default=0, ge=0, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    trace_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    error_code: str | None = None
    error_message: str | None = None
    has_extraction: bool = False
    has_summary: bool = False
    s3_prefix: str
    # Internal durable fencing and immutable artifact generations. Optional for
    # Stage 3 synthetic manifests; live Stage 4 commits always set the refs.
    worker_fence: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    retry_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    extraction_ref: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    summary_ref: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    error_diagnostic: dict[str, str | int] | None = None

    @field_validator('started_at', 'completed_at')
    @classmethod
    def optional_utc(cls, value):
        return Manifest.utc(value) if value is not None else None

    @model_validator(mode='after')
    def consistency(self):
        if self.s3_prefix != f'{self.document_id}/runs/{self.run_id}/':
            raise ValueError('Inconsistent run prefix')
        if self.cache_source_run_id == self.run_id:
            raise ValueError('Run cannot reference itself')
        if (self.extraction_ref and not self.has_extraction) or (self.summary_ref and not self.has_summary):
            raise ValueError('Artifact reference lacks committed result')
        if self.worker_fence and ((self.has_extraction and not self.extraction_ref) or (self.has_summary and not self.summary_ref)):
            raise ValueError('Live run requires committed artifact references')
        if self.has_summary and not self.has_extraction:
            raise ValueError('Summary requires extraction')
        if self.has_extraction and self.page_count is None:
            raise ValueError('Extraction requires page count')
        terminal = self.status in (JobState.completed, JobState.partial, JobState.failed)
        if terminal != (self.completed_at is not None):
            raise ValueError('Terminal status requires completion timestamp')
        if self.status == JobState.completed and not (self.has_extraction and self.has_summary):
            raise ValueError('Completed run requires extraction and summary')
        if self.status == JobState.partial and (not self.has_extraction or self.has_summary):
            raise ValueError('Partial run requires extraction without summary')
        if self.status in (JobState.partial, JobState.failed) and not self.error_code:
            raise ValueError('Failed or partial run requires an error code')
        if self.started_at and not self.created_at <= self.started_at <= self.updated_at:
            raise ValueError('Invalid start timestamp')
        if self.completed_at and not (self.started_at or self.created_at) <= self.completed_at <= self.updated_at:
            raise ValueError('Invalid completion timestamp')
        return self
