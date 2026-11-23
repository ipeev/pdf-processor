"""The key monitor is read-only, bounded, and never leaks workspace/key identity."""
import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from app.openrouter_key_monitor import OpenRouterKeyMonitor
from app.system_monitor import system_snapshot


@pytest.mark.asyncio
async def test_read_only_key_usage_budget_and_redaction(settings):
    settings.openrouter_api_key = SecretStr("sk-or-v1-FAKEPRIVATE")
    calls = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/key"
        assert request.headers["Authorization"] == "Bearer sk-or-v1-FAKEPRIVATE"
        return httpx.Response(200, json={"data": {
            "label": "sk-or-v1-DONTREVEAL", "creator_user_id": "private-user",
            "workspace_id": "private-workspace", "hash": "private-hash",
            "usage": 4.1, "usage_daily": 0.2, "usage_weekly": 0.9, "usage_monthly": 2.4,
            "limit": 10, "limit_remaining": 5.9, "limit_reset": "monthly",
            "effective_budget": {"scope": "workspace", "limit_usd": 50,
                                 "remaining_usd": 21.5, "guardrail_id": "private-guardrail",
                                 "resets_at": "2026-11-15T00:00:00Z"},
            "expires_at": "2027-01-01T00:00:00Z",
            "provider_policy": {"data_collection": "deny"},
        }})

    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    monitor = OpenRouterKeyMonitor(settings, http_client=client)
    data = await monitor.snapshot()
    assert data["status"] == "available"
    assert data["usage_daily_usd"] == 0.2
    assert data["usage_weekly_usd"] == 0.9
    assert data["usage_monthly_usd"] == 2.4
    assert data["usage_usd"] == 4.1
    assert data["key_limit_remaining_usd"] == 5.9
    assert data["budget_remaining_usd"] == 21.5
    assert data["budget_scope"] == "workspace"
    assert data["privacy_policy"] == "not_verifiable_by_key_api"
    safe = json.dumps(data)
    for forbidden in ["FAKEPRIVATE", "DONTREVEAL", "private-user", "private-workspace", "private-hash", "private-guardrail", "data_collection"]:
        assert forbidden not in safe
    assert await monitor.snapshot() == data
    assert len(calls) == 1  # many UI refreshes; one metadata GET per interval
    await client.aclose()


@pytest.mark.asyncio
async def test_metadata_auth_and_provider_errors_are_nonfatal(settings):
    settings.openrouter_api_key = SecretStr("fake-local-key")
    for code, expected in [(401, "auth_error"), (403, "auth_error"),
                           (429, "rate_limited"), (404, "unavailable"), (500, "unavailable")]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(code, json={"error": {"message": "sensitive upstream error"}})
        )) as client:
            monitor = OpenRouterKeyMonitor(settings, http_client=client)
            data = await monitor.snapshot()
            assert data["status"] == expected
            assert data["usage_usd"] is None
            assert "sensitive" not in json.dumps(data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"unexpected": []}))) as client:
        monitor = OpenRouterKeyMonitor(settings, http_client=client)
        assert (await monitor.snapshot())["status"] == "unavailable"


@pytest.mark.asyncio
async def test_unconfigured_or_disabled_key_never_calls_network(settings):
    called = 0
    def endpoint(req):
        nonlocal called
        called += 1
        raise AssertionError("Never call OpenRouter without an enabled key monitor")

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        settings.openrouter_api_key = SecretStr("")
        a = OpenRouterKeyMonitor(settings, http_client=client)
        assert (await a.snapshot())["status"] == "not_configured"
        settings.openrouter_api_key = SecretStr("test-nonsecret")
        settings.openrouter_monitor_enabled = False
        b = OpenRouterKeyMonitor(settings, http_client=client)
        assert (await b.snapshot())["status"] == "not_monitored"
    assert called == 0


@pytest.mark.asyncio
async def test_parallel_polls_share_one_request(settings):
    settings.openrouter_api_key = SecretStr("fake-local-key")
    calls = 0
    async def endpoint(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"data": {"usage": 7.1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        monitor = OpenRouterKeyMonitor(settings, http_client=client)
        result = await asyncio.gather(*[monitor.snapshot() for _ in range(8)])
    assert all(x["usage_usd"] == 7.1 for x in result)
    assert calls == 1


@pytest.mark.asyncio
async def test_monitoring_retains_infrastructure_if_key_endpoint_fails(redis_fake, settings):
    from app.queue import DocumentQueue
    settings.openrouter_api_key = SecretStr("fake-local-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403))) as client:
        monitor = OpenRouterKeyMonitor(settings, http_client=client)
        class Store:
            async def statistics(self):
                return {"objects": 0, "bytes": 0, "truncated": False}
        queue = DocumentQueue(redis_fake, "monitor:test:queue", "testgrp", "testworker")
        await queue.ensure_group()
        data = await system_snapshot(redis_fake, Store(), queue, monitor)
    assert data["redis"]["status"] == "online"
    assert data["rustfs"]["status"] == "online"
    assert data["openrouter"]["status"] == "auth_error"


@pytest.mark.asyncio
async def test_no_pseudo_credits_or_fake_policy_state(settings):
    settings.openrouter_api_key = SecretStr("fake-local-key")
    payload = {"data": {"usage": 1.5, "limit": None, "limit_remaining": None,
                        "effective_budget": None, "is_free_tier": False}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        d = await OpenRouterKeyMonitor(settings, http_client=client).snapshot()
    assert d["key_limit_usd"] is None
    assert d["budget_remaining_usd"] is None
    assert d["privacy_policy"] == "not_verifiable_by_key_api"
    assert "account_credits" not in d
    assert "provider_allowlist" not in d
