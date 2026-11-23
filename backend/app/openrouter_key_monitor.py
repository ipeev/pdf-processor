"""Safe read-only OpenRouter key usage telemetry.

Only GET /api/v1/key is used; never request management credentials, trigger
inference, expose a key prefix, or claim workspace privacy settings are verified.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import math
import time
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


def empty_key_status(status: str = "not_monitored") -> dict[str, Any]:
    return {
        "status": status,
        "checked_at": None,
        "usage_usd": None,
        "usage_daily_usd": None,
        "usage_weekly_usd": None,
        "usage_monthly_usd": None,
        "key_limit_usd": None,
        "key_limit_remaining_usd": None,
        "key_limit_reset": None,
        "budget_limit_usd": None,
        "budget_remaining_usd": None,
        "budget_scope": None,
        "budget_resets_at": None,
        "expires_at": None,
        "privacy_policy": "not_verifiable_by_key_api",
    }


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _parse_key_data(payload: Any, checked_at: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Missing OpenRouter key metadata")
    data = payload["data"]
    budget = data.get("effective_budget")
    if not isinstance(budget, dict):
        budget = {}
    result = empty_key_status("available")
    result.update({
        "checked_at": checked_at,
        "usage_usd": _amount(data.get("usage")),
        "usage_daily_usd": _amount(data.get("usage_daily")),
        "usage_weekly_usd": _amount(data.get("usage_weekly")),
        "usage_monthly_usd": _amount(data.get("usage_monthly")),
        "key_limit_usd": _amount(data.get("limit")),
        "key_limit_remaining_usd": _amount(data.get("limit_remaining")),
        "key_limit_reset": data.get("limit_reset") if data.get("limit_reset") in ("daily", "weekly", "monthly") else None,
        "budget_limit_usd": _amount(budget.get("limit_usd")),
        "budget_remaining_usd": _amount(budget.get("remaining_usd")),
        "budget_scope": budget.get("scope") if budget.get("scope") in ("key", "member", "workspace", "organization") else None,
        "budget_resets_at": _date(budget.get("resets_at")),
        "expires_at": _date(data.get("expires_at")),
    })
    return result


class OpenRouterKeyMonitor:
    """Caches GET /key responses to prevent 3-second dashboard polls hitting the API."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None):
        self._key = settings.openrouter_api_key.get_secret_value().strip()
        self._url = f"{settings.openrouter_base_url}/key"
        self._refresh_seconds = settings.openrouter_monitor_refresh_seconds
        self._client_owned = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(3.0), follow_redirects=False)
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._refresh_after: float = 0.0
        self._enabled = settings.openrouter_monitor_enabled

    async def aclose(self) -> None:
        if self._client_owned:
            await self._client.aclose()

    async def snapshot(self) -> dict[str, Any]:
        if not self._enabled:
            return empty_key_status("not_monitored")
        if not self._key:
            return empty_key_status("not_configured")
        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and now < self._refresh_after:
                return self._cached.copy()
            # Even failures are cached: an unreachable provider won't cause
            # repeated 3-second outbound calls from the dashboard.
            self._refresh_after = now + self._refresh_seconds
            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                response = await self._client.get(
                    self._url, headers={"Authorization": "Bearer " + self._key},
                    timeout=3.0,
                )
                if response.status_code == 200:
                    result = _parse_key_data(response.json(), checked_at)
                else:
                    state = {401: "auth_error", 403: "auth_error", 429: "rate_limited"}.get(
                        response.status_code, "unavailable"
                    )
                    result = empty_key_status(state)
                    log.info("OpenRouter key metadata check returned HTTP %s", response.status_code)
            except (httpx.HTTPError, ValueError, TypeError):
                result = empty_key_status("unavailable")
                log.info("OpenRouter key metadata check unavailable (no request details logged)")
            self._cached = result
            return result.copy()
