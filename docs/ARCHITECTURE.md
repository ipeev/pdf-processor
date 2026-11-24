# Architecture — PDF Processor 0.12.7

This document describes the **currently implemented** system. Diagrams use native GitHub Mermaid Markdown. It is a single-host, trusted-user Docker Compose architecture; it is not a public multi-tenant service.

## Runtime services and trust boundaries

```mermaid
flowchart TB
    User["Browser / trusted operator"]
    subgraph Host["Linux host — externally visible ports bound to 127.0.0.1"]
      subgraph Compose["Docker Compose application"]
        Web["Nginx + React<br/>:3000"]
        API["FastAPI<br/>:8000"]
        W["Two worker containers<br/>20 consumers per container by default"]
        Redis[("Redis Streams<br/>jobs + volatile cache")]
        RustFS[("RustFS<br/>permanent and temporary object buckets")]
        Search[("OpenSearch<br/>metadata + BM25 passages")]
        Coord[("Archive coordination volume<br/>flock")]
        Web -->|"/api/*"| API
        API <-->|"enqueue / status / cache"| Redis
        W <-->|"claim / heartbeat / ACK"| Redis
        API <-->|"read / write"| RustFS
        W <-->|"immutable results + figures"| RustFS
        API <-->|"metadata / full-text"| Search
        W -->|"run + passage projection"| Search
        API -.-> Coord
        W -.-> Coord
      end
    end
    User --> Web
    W -.->|"live AI extraction + summary"| Router["OpenRouter · external, paid"]
    API -.->|"optional key usage metadata"| Router
    API -.->|"optional OTLP"| Telemetry["External observability stack · not included"]
    W -.-> Telemetry
```

`frontend` exposes 127.0.0.1:3000 and proxies `/api/*`. `api` exposes 127.0.0.1:8000. RustFS exposes 127.0.0.1:9000/9001. Redis has no host port. OpenSearch runs on an internal Compose network without a published port; its security plugin is disabled. API/workers run as UID 10001, with dropped capabilities and no privilege escalation. A one-shot constrained initializer prepares named volume ownership.

The app currently uses Redis 7.4, RustFS 1.0.3, OpenSearch 3.10.0, Python 3.15.2 (backend), and Node 22/React 19 (frontend). Container and environment versions are defined by `docker-compose.yml` and the Dockerfiles.

## Upload, processing, and result lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Browser
    participant API as FastAPI
    participant S3 as RustFS
    participant R as Redis Streams
    participant Worker
    participant Search as OpenSearch
    participant AI as OpenRouter
    Browser->>API: POST /api/v1/documents (PDF/ZIP, parser)
    API->>API: Validate input, SHA-256 document, run UUID
    API->>S3: Save canonical original and queued run manifest
    API->>Search: Publish document/run projection
    API->>R: Cache/dedup decision and enqueue
    API-->>Browser: 202 Accepted with run IDs
    Worker->>R: Claim work with ownership token
    alt Reusable committed extraction
      Worker->>S3: Validate and produce run-owned results
    else Extraction required
      Worker->>S3: Read/verify source PDF
      alt pypdf
        Worker->>Worker: Extract embedded page text
      else AI parser
        Worker->>AI: Uncached page extraction
        AI-->>Worker: Page Markdown, optional figure boxes
        Worker->>Worker: Validate output and generate figure PNGs
      end
      Worker->>S3: Commit extracted pages and figure assets
    end
    Worker->>AI: Request summary unless cached
    AI-->>Worker: Summary, or error
    Worker->>S3: Commit terminal run manifest
    Worker->>Search: Publish terminal metadata projection
    Worker->>Search: Reindex committed page passages (best effort)
    Worker->>R: Record status and ACK owned job
    Browser->>API: Poll/run detail/pages/images/download
    API->>S3: Read verified committed artifacts
    API-->>Browser: Status, Markdown, images and ZIP
```

Worker ownership, heartbeat, fenced manifest commits, and per-page model-call admission/checkpoint mechanisms limit duplicate work and help recovery. They do **not** guarantee exactly-once provider billing if a remote response is lost. A failed summary can leave a `partial` run with extracted text accessible and eligible for an explicit summary-only retry; extraction failure is recorded separately. Polling errors are not treated as empty history.

## Dedicated image / figure extraction flow

```mermaid
flowchart LR
    Page["PDF page"] --> Render["PDFium raster render"]
    Render --> Model["One full-mode vision request<br/>Markdown + figure boxes"]
    Model --> Verify["Validate response, marker IDs,<br/>shape and table exclusion"]
    Verify --> Crop["Crop PNG from original PDF pixels<br/>with bounded padding/clearance"]
    Verify --> Markdown["Ordered Markdown<br/>caption-aware figure links"]
    Crop --> Hash["Image metadata + SHA-256"]
    Markdown --> Archive["Immutable run generation in RustFS"]
    Hash --> Archive
    Archive --> Http["Run-owned same-origin PNG API"]
    Http --> UI["React preview / History"]
    Archive --> Export["ZIP: document.md + figures.json + images/"]
```

Model-generated page geometry is approximate, not authoritative: figures may be clipped, captions can be duplicated into pixel crops, and OCR on dense scans can be incomplete. Tables are intended to remain text/Markdown rather than be treated as picture assets. These limitations are documented in [the roadmap](ROADMAP.md) and covered only partially by offline fixtures.

## Object, index, and coordination ownership

| Data | Owner | Expected behavior |
|---|---|---|
| Original PDF | RustFS permanent archive | Identified by SHA-256 of source bytes |
| Document metadata | RustFS | Revisions, display names, preferred run, deletion tombstones |
| Run metadata | RustFS | Each submission has its own UUID, status, parser, artifact references |
| Page Markdown / summary | RustFS immutable run generation | Can be retained for partial runs and explicit summary retry |
| Figure manifest / PNG | RustFS run-owned artifacts | Hash-verified same-origin access and ZIP export |
| Search projections | OpenSearch | Derived document/run lists; rebuildable from archive |
| Pending jobs / cache / telemetry | Redis | Volatile; no source PDF durability guarantee |
| Archive maintenance coordination | Local named volume | Shared `flock`, single host, cooperative services only |

OpenSearch indexed lists may temporarily lag an authoritative RustFS read. Maintenance can reconcile durable state and rebuild indexes. Backups must include RustFS archive contents; preserving only the OpenSearch volume is insufficient.

## Failure boundaries

- **Redis restart:** in-memory cache, queues, and monitoring state can be lost. The durable archive and recovery/reconciliation mechanisms are separate; not every in-flight call can be replayed without risk.
- **Worker crash:** another worker can claim abandoned messages and reuse durable checkpoints, subject to ownership/fencing and paid-call ambiguity.
- **OpenSearch outage:** discovery is unavailable or degraded; direct authorized run/document reads can still use RustFS. No silent empty-result fallback.
- **RustFS outage / loss:** authoritative documents and result assets are unavailable; volume removal is data loss unless restored from backup.
- **OpenRouter error / timeout:** a run can fail or remain partial, with bounded diagnostics. The test suite cannot certify live model accuracy, provider availability, or exact billed cost.
- **Container recreation:** expected to preserve named volumes, but may interrupt jobs. `docker compose down -v` removes all named volumes and must not be used for routine maintenance.

See [API contract](PERSISTENT_API.md), [operations](OPERATIONS.md), [configuration](CONFIGURATION.md), and [security](../SECURITY.md).


## Full-text search pipeline

```mermaid
flowchart LR
    Archive[("RustFS · pages.json / manifests")] -->|"committed, completed/partial runs"| Split["220-word page-local passages"]
    Split --> Index[("OpenSearch · pdf-passages-v1")]
    Browser["React Search tab"] -->|"POST /api/v1/search"| API["FastAPI Search API"]
    API -->|"validated BM25 DSL"| Index
    Index -->|"collapse document_id + top inner_hits"| API
    API -->|"one PDF + page/run snippets"| Browser
    Browser -->|"/runs/{id}?page=N"| Pages["Archived run page viewer"]
```

The passage index is a derived, resettable read model. Document SHA-256 groups runs under one PDF, while run UUID plus page and passage number preserve navigation. The worker indexes after committing the terminal run; an offline CLI rebuild uses the same immutable RustFS artifacts without making any model calls. See [Full-text search](FULL_TEXT_SEARCH.md) for matching, scoring, initial backfill, and current consistency limitations.
