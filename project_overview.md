# Project overview — current system

**PDF Processor 0.12.7** is a self-hosted document-processing application built on FastAPI, React, Redis Streams, S3-compatible RustFS, OpenSearch, and Docker Compose. This document describes **the implemented system as shipped**, not its development history. The source of truth for exact behavior remains the application code and executable tests.

## Intended use and supported environment

- A trusted Linux machine with Docker Engine / Docker Compose v2; single-user/local-network workflow, no authentication or TLS termination included.
- Two worker containers by default, each supervising `WORKER_THREADS=20` consumers, with bounded CPU-independent container memory limits. Exposed HTTP and RustFS ports bind to `127.0.0.1`.
- Python 3.15.2 API/workers (locked through `backend/uv.lock`) and Node 22 React/Vite frontend (locked through `frontend/package-lock.json`).
- OpenRouter credentials are optional for boot and offline testing, but required for real summarization and model-dependent extraction. Models and limits are set via `.env`.
- Synthetic fixtures and mocked provider calls are used in CI. This is not a multi-tenant, authenticated, or production-hardened hosted service.

## Supported user workflows

1. **Upload.** Accept up to five PDFs or one ZIP archive with up to 20 PDF entries, subject to configurable PDF byte/page limits. The API allocates a run UUID and uses the original bytes' SHA-256 as the canonical document identity.
2. **Choose an extraction method.** `pypdf` extracts embedded text locally; `gemini` sends native PDF content to a configured AI model; `gpt5_mini` renders pages for image-based OCR; `vision_figures` requests Markdown with figures and stores actual PDF crops. Parser IDs are persistent API values, not guarantees of a specific current provider model.
3. **Process asynchronously.** Redis Streams coordinates worker claims, heartbeats, and retries. Durable run metadata and stage outcomes are kept in RustFS, with cache/dedup handling and fences intended to avoid duplicate paid requests after uncertain outcomes.
4. **Read results.** Upload shows active jobs. History and Library show persisted run/document metadata, including completed, partial, and failed runs. Page Markdown, summary, source PDF, and image assets are accessible through API endpoints.
5. **Search extracted content.** The Search view queries a rebuildable OpenSearch BM25 passage index, groups multiple matching pages and processing runs by canonical PDF, shows highlighted excerpts, and links to the exact run/page. Search can be restricted to the document's preferred extraction or expanded to all extraction runs. Existing RustFS text can be indexed offline without calling OpenRouter.
6. **Organize documents.** The Library groups runs by source PDF hash, allows document display-name changes and a preferred completed run. History lists independent runs and supports filters/cursors.
7. **Export or remove.** Download Markdown/summary and figure bundles; delete individual runs or cascade-delete a document with the required precondition. Maintenance commands support reconciliation, index rebuild, backup, and restore.
8. **Monitor.** `/system` reports live and nullable service/worker/archive measurements, optional Docker telemetry, optional OpenRouter key usage metadata, and trace links when independently configured observability infrastructure exists.

## Canonical data and failure model

| Component | Responsibility | Durability |
|---|---|---|
| RustFS `pdf-documents` bucket | Original bytes, per-document metadata, immutable run generations, extracted pages, summaries, figure manifests and PNGs | **Authoritative** on a Docker named volume; requires backup |
| RustFS temporary upload bucket | Pending upload objects, with configurable short expiration | Temporary |
| Redis | Queued/claimed jobs, cache entries, heartbeat and transient status | **Volatile**; restarts may discard in-memory state |
| OpenSearch | Searchable document/run projections for History and Library | Rebuildable from canonical RustFS manifests |
| `archive_coordination` volume | Local cross-container advisory lock for archive maintenance | Single-host coordination, not a distributed security system |
| OpenRouter | External extraction and summary service for configured AI stages | Not part of the self-hosted stack; inference is billable |

The implementation distinguishes a recoverable failed **summary** (a `partial` result with saved extraction) from an extraction failure. Redis outages, worker crashes, OpenSearch downtime, and inconsistent durable state are addressed by ownership fencing, reconciliation, and explicit maintenance. It cannot guarantee exactly-once provider billing in all crash windows; the design favors *not silently resubmitting* an uncertain paid call.

## Figures implementation

The dedicated fourth parser `vision_figures` obtains Markdown and figure boxes in a model request per page (with bounded retries of definitively rejected replies), enforces configurable page/asset limits, and crops images from the underlying PDF using PDFium rather than accepting model-generated image bytes. Figure metadata and asset SHA-256 values are committed with each run; the UI serves only verified run-owned PNGs through same-origin routes. A portable ZIP contains Markdown, a figure manifest, and relative image paths. Cache/shared runs can have independent image ownership.

**Known quality limits:** caption text can leak into crops; figures can be clipped or misplaced; text can be incomplete on dense scanned pages; model metadata and bounding boxes can be inaccurate. These are accuracy issues, not an indication that the persistence architecture is unimplemented. Offline fixture tests do not prove quality on arbitrary PDFs.

## Important non-goals and security limits

- No built-in accounts, permissions by user, OAuth, reverse-proxy authentication, or TLS gateway.
- No public-facing OpenSearch security configuration; the compose network is internal and the plugin is disabled.
- No promise of exhaustive OCR or exact table/figure boundaries.
- No auto-funded AI testing, hosted OpenRouter inference, Grafana/Loki/Tempo deployment, or elastic scale-out across multiple hosts.
- No durability guarantee against `docker compose down -v`, volume loss, or untested restore procedures. Backups are required.

## Verification and maintenance

Run `./scripts/test_release.sh --offline` (never omit `--offline` for a free test), `./scripts/check_figures.sh --offline`, and `./scripts/check_regressions.sh` on a disposable/idle Docker test stack. GitHub Actions run source/frontend checks and Docker-hosted offline tests without API secrets; CodeQL and Dependabot are separately configured. Live OpenRouter behavior and visual crop quality require optional manual review.

**Current technical references:** [Full-text search](docs/FULL_TEXT_SEARCH.md) · [Architecture](docs/ARCHITECTURE.md) · [API](docs/PERSISTENT_API.md) · [Configuration](docs/CONFIGURATION.md) · [Operations](docs/OPERATIONS.md) · [Testing](docs/CI_AND_TESTING.md) · [Potential next steps](docs/ROADMAP.md).
