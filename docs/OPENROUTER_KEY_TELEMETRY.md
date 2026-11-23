# OpenRouter key usage telemetry — current implementation

The **System Monitor** at `http://localhost:3000/system` includes a read-only OpenRouter API-key usage card. It uses the **same workspace-scoped `OPENROUTER_API_KEY`** as document processing and the optional PDF benchmark.

## Endpoint and displayed metrics

The FastAPI service calls `GET {OPENROUTER_BASE_URL}/key` with the existing bearer token. The implementation uses this endpoint with **ordinary API keys**; it does not require a management key. The app extracts only the following fields when supplied:

| Display | Upstream `GET /key` response field |
| --- | --- |
| Total/daily/weekly/monthly usage (USD) | `data.usage`, `data.usage_daily`, `data.usage_weekly`, `data.usage_monthly` |
| Key-specific spending cap/remaining amount | `data.limit`, `data.limit_remaining` |
| Key-specific cap reset | `data.limit_reset` |
| Most restrictive applicable budget's limit/remaining | `data.effective_budget.limit_usd`, `.remaining_usd` |
| Budget scope and next reset | `data.effective_budget.scope`, `.resets_at` |
| Key expiry | `data.expires_at` |

All fields are nullable. **No key cap reported does NOT mean infinite account credits.** Usage numbers describe the key; the `effective_budget` field may refer to a workspace, member or guardrail budget. Reported budget headroom is not the same thing as OpenRouter account credit balance.

The separate `GET /api/v1/credits` endpoint requires a **management key** in current OpenRouter documentation. We deliberately do not request, store, or use a management key. The key-monitoring feature does not show account-wide credit balance.

## Request frequency and failure behavior

`/api/v1/system/status` is called every three seconds by the frontend. FastAPI **caches key metadata for 60 seconds** by default and shares one cached result between concurrent requests. It imposes a three-second HTTP timeout and does not follow redirects. A failed check is cached for the same interval. No inference requests are used in this telemetry path and there are **no OpenRouter inference charges for GET /key**.

If the key is absent, telemetry is disabled, the credential is rejected, rate-limited, or OpenRouter is unavailable, the dashboard shows a dedicated status and unknown metrics (`—`). The Redis, RustFS and worker sections continue working.

Configuration in `.env.example`:

```dotenv
OPENROUTER_MONITOR_ENABLED=true
OPENROUTER_MONITOR_REFRESH_SECONDS=60
```

`OPENROUTER_MONITOR_ENABLED=false` disables remote metadata requests entirely. The refresh period is constrained to 30–3600 seconds.

## Workspace privacy and provider restrictions

**The workspace's restrictions remain in force.** The production and benchmark request payloads do not weaken or override provider allowlists, training/data-collection restrictions, or Zero Data Retention requirements. These are enforced upstream by OpenRouter's account/workspace/guardrail routing rules. In particular, when no provider/model endpoint satisfies these restrictions, an inference request can fail with **HTTP 404** even when the model appears in the public model catalog. The benchmark records the sanitized error reason and can skip an unavailable model with `--on-http-404 skip-model`.

**Important limitation:** `GET /key` does **not** return a verified no-training or ZDR policy state. The UI explicitly says the privacy policy is *not independently verifiable through this endpoint*. It must not display a fabricated “privacy compliant” badge. To confirm your restrictions, inspect them in the OpenRouter workspace/account Privacy and Guardrails settings.

The monitoring response is allowlisted: it excludes API key, key label, hashes, creator IDs, workspace IDs, guardrail IDs, allowed provider lists, request prompts, PDF data, and OpenRouter error-response bodies. It shows only numeric usage/limits, expiration and service status. The dashboard is intended for **localhost-only** access; there is no authentication for `/system/status`.

## Diagnostics and limitations

- `available`: key metadata GET succeeded. **Does not** mean any particular model, provider or inference route is online.
- `auth_error`: upstream returned HTTP 401 or 403.
- `rate_limited`: upstream returned HTTP 429.
- `unavailable`: timeout, connection failure, malformed response or another HTTP error.
- `not_configured`: no application API key.
- `not_monitored`: monitoring disabled (or not initialized in an isolated test).

No account or provider diagnostics are inferred from Redis queue status. Each configured provider can have independent restrictions and availability.

## Docs

- OpenRouter current key: <https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key>
- OpenRouter credits (management key required): <https://openrouter.ai/docs/api/api-reference/credits/get-credits>
- Guardrail hierarchy and provider restrictions: <https://openrouter.ai/docs/guides/features/guardrails/overview>
- Privacy/data collection and ZDR: <https://openrouter.ai/docs/guides/get-started/sovereign-ai>
