"""Small allowlist for safe provider failure metadata passed to the UI.

Provider strings are untrusted; never store raw error bodies or free-form text.
"""
from __future__ import annotations

import re
from typing import Any

# Only these fields may be persisted. All others are dropped.
_TEXT = {"phase": 24, "kind": 24, "requested_model": 120,
         "response_model": 120, "finish_reason": 64,
         "native_finish_reason": 64, "response_id": 120}
_INT = {"page", "pages_total", "max_tokens", "prompt_tokens",
        "completion_tokens", "total_tokens", "http_status", "attempt", "max_attempts"}
_SAFE = re.compile(r"^[a-zA-Z0-9_./:+-]+$")


def sanitize_diagnostic(value: Any) -> dict[str, str | int]:
    """Never turn provider free text into customer-visible diagnostics."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, str | int] = {}
    for key, maximum in _TEXT.items():
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= maximum and _SAFE.fullmatch(item):
            result[key] = item
    for key in _INT:
        item = value.get(key)
        if type(item) is int and 0 <= item <= 1_000_000_000:
            result[key] = item
    return result


def provider_diagnostic(payload: dict, *, phase: str, attempt: int, max_attempts: int,
                        kind: str, page: int | None = None,
                        response: Any = None, body: Any = None,
                        finish_reason: Any = None) -> dict[str, str | int]:
    data: dict[str, Any] = {
        "phase": phase, "kind": kind, "requested_model": payload.get("model"),
        "max_tokens": payload.get("max_tokens"), "attempt": attempt,
        "max_attempts": max_attempts, "page": page,
    }
    if response is not None:
        data["http_status"] = response.status_code
    if isinstance(body, dict):
        data["response_model"] = body.get("model")
        data["response_id"] = body.get("id")
        usage = body.get("usage")
        if isinstance(usage, dict):
            data.update({key: usage.get(source) for key, source in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            )})
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            data["native_finish_reason"] = choices[0].get("native_finish_reason")
    data["finish_reason"] = finish_reason
    return sanitize_diagnostic(data)