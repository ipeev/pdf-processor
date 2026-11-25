"""Bounded vision retries: only known-completed responses may be repeated.

A transport timeout or a missing durable response has an uncertain billable
outcome. These are never retried automatically. HTTP 429/5xx with a received
response, or an explicitly invalid response, can safely consume a new attempt.
"""
from __future__ import annotations

import asyncio

from app.errors import ProcessingError


RETRYABLE_PROVIDER = frozenset({
    "PROVIDER_BAD_RESPONSE", "PROVIDER_EMPTY_RESPONSE",
    "PROVIDER_INCOMPLETE", "PROVIDER_TRUNCATED",
})


def known_failure(exc: ProcessingError) -> bool:
    if exc.code in RETRYABLE_PROVIDER or exc.code == "VISION_RESPONSE_INVALID":
        return True
    return (exc.code == "PROVIDER_UNAVAILABLE"
            and isinstance(exc.diagnostic, dict)
            and exc.diagnostic.get("kind") == "http"
            and (exc.diagnostic.get("http_status") == 429
                 or isinstance(exc.diagnostic.get("http_status"), int)
                 and exc.diagnostic["http_status"] >= 500))


async def retry_delay(attempt: int) -> None:
    # No sleep after the final attempt. Limited backoff avoids hammering a
    # model that is generating malformed output or throttling requests.
    await asyncio.sleep(min(2 ** (attempt - 1), 4))
