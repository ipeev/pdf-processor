# Configuration — PDF Processor 0.12.6

Copy `.env.example` to `.env` and edit the local file; it is ignored by Git. Never commit real keys or environment dumps. `scripts/setup.sh` preserves an existing `.env`. Docker Compose overrides selected internal service URLs regardless of the values in `.env`.

## AI providers and parser selection

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Required for real summarization and AI extraction; **not** required for CI/offline tests |
| `OPENROUTER_BASE_URL` | Provider API base, default OpenRouter |
| `NATIVE_PDF_MODEL` | Model for `gemini` parser compatibility ID |
| `IMAGE_OCR_MODEL` | Model for `gpt5_mini` parser compatibility ID |
| `VISION_FIGURES_MODEL` | Full-page Markdown + image-box model for `vision_figures` |
| `VISION_FIGURES_PAGE_ATTEMPTS` | Up to 3 attempts for clearly rejected page responses; unknown-outcome calls are not repeated automatically |
| `VISION_LOG_INVALID_RESPONSE` | **Off by default**. Log the complete rejected `message.content` and, for live responses, the full OpenRouter HTTP response in worker logs. **Contains document text**; use briefly and turn off. |
| `SUMMARY_MODEL` | Summary generation model, including after local `pypdf` extraction |
| `OPENROUTER_TIMEOUT_SECONDS` | Request timeout |
| `OPENROUTER_MAX_CALL_ATTEMPTS` | Configured per-call attempt limit; not a guarantee of duplicate-free billing |
| `RUN_LIVE_TESTS` | Leave `0` for offline verification |

These parser names are stored in durable run metadata; do not rename them casually. Check provider model availability, cost and data handling policies before enabling paid processing. Monitoring metadata is separate from inference success.

## Size, page and image constraints

| Variable | `.env.example` value | Meaning |
|---|---:|---|
| `MAX_FILES_PER_REQUEST` | 5 | Maximum PDFs in a normal upload |
| `MAX_FILE_SIZE_MIB` | 20 | Per-PDF size ceiling |
| `MAX_TOTAL_UPLOAD_MIB` | 50 | Aggregate upload ceiling |
| `MAX_PAGES_PER_DOCUMENT` | 200 | General page limit |
| `MAX_AI_PAGES_PER_DOCUMENT` | 200 | AI parser page limit |
| `MAX_VISION_FIGURES_PAGES_PER_DOCUMENT` | 50 | Figure parser page limit |
| `MAX_VISION_FIGURES_PER_PAGE` | 8 | Figure count limit per page |
| `MAX_VISION_ASSET_BYTES` | 6000000 | Maximum bytes per image asset |
| `MAX_VISION_TOTAL_ASSET_BYTES` | 100000000 | Total figure bytes ceiling |
| `VISION_FIGURE_PADDING_NORM` | 20 | Normalized padding for crops; does not guarantee accurate bounds |
| `MAX_RENDER_PIXELS` | 12000000 | Renderer safety limit |
| `MAX_AI_OUTPUT_TOKENS_PER_PAGE` | 32768 | General AI page output budget |
| `VISION_FIGURES_MAX_TOKENS_PER_PAGE` | 16000 | Figure parser full-mode page budget |
| `MAX_SUMMARY_INPUT_CHARS` | 400000 | Summary text budget |
| `MAX_SUMMARY_OUTPUT_TOKENS` | 4096 | Summary output token budget |

ZIP uploads are limited to a single archive with at most 20 PDF members; size/page checks also apply after expansion.

## Storage and concurrency

| Variable | Function |
|---|---|
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Local RustFS credentials; **replace demonstration secret** |
| `S3_BUCKET` | Temporary upload bucket (default `pdf-uploads`) |
| `S3_ARCHIVE_BUCKET` | Durable archive bucket (default `pdf-documents`) |
| `S3_LIFECYCLE_DAYS` | Temporary upload expiration (does not expire the permanent archive) |
| `REDIS_URL`, `OPENSEARCH_URL`, `S3_ENDPOINT_URL` | Internal Docker service addresses |
| `WORKER_THREADS` | Concurrent worker consumers per worker container (example: 20; default two containers) |
| `CACHE_TTL_SECONDS`, `RESULT_TTL_SECONDS` | Volatile Redis retention |
| `WORKER_POLL_MS`, `WORKER_HEARTBEAT_SECONDS`, `WORKER_CLAIM_IDLE_SECONDS` | Redis Streams ownership parameters |

RustFS and OpenSearch use named Docker volumes; Redis has no persistent append-only or snapshot storage. The default Compose file caps API/frontend/Redis/RustFS/OpenSearch containers at 2 GiB and each worker at 4 GiB, with resource contention dependent on host memory.

## Monitoring and logs

- `OPENROUTER_MONITOR_ENABLED`, `OPENROUTER_MONITOR_REFRESH_SECONDS`: optional key-scoped usage/budget card; not account credit balance.
- `DOCKER_TELEMETRY_ENABLED`, `DOCKER_TELEMETRY_TOKEN`: optional local Docker metrics collector; `setup.sh` creates a random token if absent.
- `OBSERVABILITY_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`: export to an independently configured OTLP destination; Grafana/Loki/Tempo not bundled.
- `GRAFANA_PUBLIC_URL`, `GRAFANA_TEMPO_DATASOURCE_UID`: optional browser trace links; frontend needs rebuild if changed.
- `LOG_FORMAT`, `LOG_LEVEL`: structured runtime output. Compose configures bounded Docker JSON-log rotation.

For exact defaults and additional flags, refer to [`.env.example`](../.env.example) and the validation in `backend/app/config.py`. See [OpenRouter key telemetry](OPENROUTER_KEY_TELEMETRY.md) and [operations](OPERATIONS.md).
