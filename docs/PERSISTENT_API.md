# HTTP API and persisted resources — PDF Processor 0.12.8

The FastAPI route prefix is `/api/v1`. The browser reaches it through the frontend's same-origin `/api/*` proxy on port 3000; it can also be accessed on localhost port 8000. **There is no application authentication.** Keep the service inside a trusted security boundary.

## Identifiers and persisted results

- `document_id`: SHA-256 of the exact original PDF bytes (lowercase 64-hex). Different uploads of the same bytes share a document identity.
- `run_id`: a distinct UUID per processing submission, including cache-hit/shared work. ZIP member PDFs receive independent runs.
- Parser contract: `pypdf`, `gemini`, `gpt5_mini`, `vision_figures` (compatibility IDs; models are configurable).
- Run states: `queued`, `processing`, `summarizing`, `completed`, `partial`, `failed`. A `partial` result has committed extraction but failed/incomplete summarization.
- RustFS is canonical for documents/runs; OpenSearch provides rebuildable list views. Redis retains short-lived coordination and cache entries.

## Upload and active-run endpoints

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/health` | API liveness |
| `GET` | `/ready` | Readiness including required service checks |
| `POST` | `/documents` | Multipart `files` and `parser`, returns 202 with accepted document/run entries; supports normal PDF and ZIP ingestion |
| `GET` | `/documents/{run_uuid}` | Poll a submission's status/details |
| `GET` | `/documents/{run_uuid}/pages` | Ordered Markdown pages when extraction is available |
| `POST` | `/documents/{run_uuid}/retry-summary` | Explicit summary-only retry for eligible partial runs, 202 |
| `GET` | `/system/status` | Service, worker, archive and optional OpenRouter-key monitoring snapshot |

Upload limits in the example configuration are five PDFs or one ZIP with at most 20 PDFs; each PDF <=20 MiB, aggregate upload <=50 MiB. Configured page limits apply, including a separate cap for `vision_figures`.

## Permanent archive and catalogue routes

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/library` | Paged unique document records |
| `GET` | `/library/{document_id}` | Canonical document details |
| `GET` | `/library/{document_id}/runs` | Paged runs for a document |
| `GET` | `/library/{document_id}/original` | SHA-verified original PDF attachment |
| `PATCH` | `/library/{document_id}` | Rename display name: `{"display_name":"name.pdf"}` |
| `PATCH` | `/library/{document_id}/preferred-run` | Select completed run: `{"run_id":"<uuid>"}` |
| `GET` | `/runs` | Paged independent run records / History |
| `GET` | `/runs/{run_uuid}` | Canonical persisted run details and committed summary |
| `GET` | `/runs/{run_uuid}/pages` | Ordered Markdown page array |
| `GET` | `/runs/{run_uuid}/markdown` | Exact Markdown download |
| `GET` | `/runs/{run_uuid}/summary` | Summary Markdown download |
| `GET` | `/runs/{run_uuid}/figures` | Figure manifest; empty manifest for other parsers |
| `GET` | `/runs/{run_uuid}/images/{filename}` | Verified run-owned PNG; filename is server-validated |
| `GET` | `/runs/{run_uuid}/bundle.zip` | Portable `document.md`, `figures.json`, `images/` for committed figure extraction |
| `POST` | `/runs/{run_uuid}/retry-summary` | Retry eligible partial-run summary, 202 |
| `DELETE` | `/runs/{run_uuid}` | Delete terminal run, retain source document and other runs |
| `DELETE` | `/library/{document_id}` | Cascade delete document, original and runs; requires `If-Match: "<revision>"` |

Document metadata includes display name/aliases, SHA-256, size, preferred run, timestamps and revision. Run metadata includes run ID, document ID, parser, status/cache, models, trace ID, costs/tokens when known, artifacts and error diagnostics. Sensitive internal storage paths, raw provider bodies, and worker lease tokens are not part of the public contract.

## Passage-level full-text search

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/search` | Bounded BM25 query, collapses matches by canonical PDF; returns top matching passages and `has_more` |
| `POST` | `/search/{document_id}/matches` | Paged matching passages within one PDF, with page, parser and run navigation |

Both routes accept a JSON body containing `query` (2–200 characters), `match` (`all` / `any` / `phrase`), `scope` (`preferred` / `all`), optional `parser`, `sort` (`relevance` / `newest`), `offset` (0–2000) and `limit` (1–20). No raw OpenSearch DSL is allowed. Search is an offline derived index, not an OpenRouter request. `total_documents: null` means the grouped document count is not asserted; `matching_passages` counts matching index passages, not distinct pages. Snippets are untrusted PDF text and must be rendered as plain text and explicit highlight spans, never raw HTML. Read the [full API example and index model](FULL_TEXT_SEARCH.md).

## Filtering and paging

The list responses use `{"items": [...], "next_cursor": "opaque-or-null"}`. `limit` defaults to 50 (allowed 1–100). `sort` accepts `newest` and `oldest`; `filename` is case-insensitive substring matching, not raw OpenSearch DSL. Additional filters include `parser`, `status`, `created_from`, `created_to`, exact `document_id` for runs and exact `sha256` for Library. The parser filter accepts all four identifiers. Cursor tokens are opaque and bound to the original filter set; do not parse or modify them. Results can change during concurrent writes; restart from page one when necessary.

```bash
curl -fsS 'http://localhost:3000/api/v1/runs?status=completed&limit=10'
curl -fsS 'http://localhost:3000/api/v1/library?parser=vision_figures&sort=newest'
```

## Deletion, consistency and error behavior

Deletion requires an idle archive/queue and appropriate maintenance coordination. A document cascade requires the current quoted revision in `If-Match`; a stale revision fails rather than deleting a changed document. Deleting one run must not invalidate separate run-owned image assets. Deletions create tombstones to prevent stale index projection from resurrecting removed data. Do not automatically retry a destructive request on an ambiguous error.

- `202`: accepted upload or summary retry.
- `204`: successful delete / telemetry ingestion where applicable.
- `404`: canonical resource not found.
- `409`: invalid state, busy operation, or unavailable result/retry.
- `412` / `428`: stale / missing revision precondition for document deletion.
- `413` / `415` / `422`: upload limit, unsupported media, or invalid input.
- `503`: required storage/index unavailable or consistency check cannot complete. Never reinterpret this as an empty History result.

Application domain errors use bounded `{ "error": { "code": "...", "message": "..." } }`; framework validation errors can use FastAPI's default `detail` shape. Figure outputs are restricted to known committed, checksum-validated assets; ZIP paths and response headers are sanitized.

## Archive maintenance

Bulk backup, reconciliation, and catalogue rebuild are intentionally CLI operations, **not** unauthenticated administrative REST routes. See [Operations](OPERATIONS.md), [Architecture](ARCHITECTURE.md), and the FastAPI OpenAPI schema at `http://localhost:8000/openapi.json` for the complete live schema.
