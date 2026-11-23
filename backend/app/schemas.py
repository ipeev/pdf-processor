"""Public API types. Page numbering starts at 1."""
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class ParserChoice(StrEnum):
    pypdf = "pypdf"
    gemini = "gemini"
    gpt5_mini = "gpt5_mini"
    vision_figures = "vision_figures"


class JobState(StrEnum):
    queued = "queued"
    processing = "processing"
    summarizing = "summarizing"
    partial = "partial"
    completed = "completed"
    failed = "failed"


class DocumentAccepted(BaseModel):
    id: UUID
    document_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    filename: str
    parser: ParserChoice
    status: JobState = JobState.queued
    cache: str = "miss"


class UploadAccepted(BaseModel):
    documents: list[DocumentAccepted]


class SafeError(BaseModel):
    code: str
    message: str
    diagnostic: dict[str, str | int] | None = None


class DocumentStatus(BaseModel):
    id: UUID
    filename: str
    parser: ParserChoice
    status: JobState
    page_count: int | None = None
    summary: str | None = None
    cache: str = "miss"
    trace_id: str | None = None
    upload_id: UUID | None = None
    error: SafeError | None = None
    created_at: datetime
    updated_at: datetime


class Page(BaseModel):
    page: int = Field(ge=1)
    markdown: str


class PageList(BaseModel):
    id: UUID
    pages: list[Page]


class ErrorResponse(BaseModel):
    error: SafeError


class SummaryRetryAccepted(BaseModel):
    id: UUID
    status: JobState
