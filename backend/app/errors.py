"""Sanitized errors shared across API and background worker."""
from dataclasses import dataclass
from typing import Any


@dataclass
class ProcessingError(Exception):
    code: str
    message: str
    http_status: int = 422
    # Bounded, allowlisted metadata only. Never include prompts, documents,
    # headers, response bodies or provider-supplied free-form error messages.
    diagnostic: dict[str, Any] | None = None

    def with_context(self, **context: int | str) -> "ProcessingError":
        from app.provider_diagnostics import sanitize_diagnostic
        self.diagnostic = sanitize_diagnostic({**(self.diagnostic or {}), **context})
        return self

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class EnqueueOutcomeUnknown(ProcessingError):
    def __init__(self) -> None:
        super().__init__(
            "UPLOAD_OUTCOME_UNKNOWN",
            "Could not verify whether the upload was queued. The Redis transaction may have committed. Retrying could create duplicate jobs.",
            503,
        )


def no_text() -> ProcessingError:
    return ProcessingError("NO_EXTRACTABLE_TEXT", "No extractable document text was found.")
