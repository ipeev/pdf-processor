# Installation and local operation — PDF Processor 0.12.3

## Prerequisites

Linux with Docker Engine, Docker Compose v2, Bash, network access to pull dependency images, and free RAM/disk for Redis, RustFS, OpenSearch, API, two workers and the frontend. Docker daemon access is privileged. OpenSearch requires `vm.max_map_count >= 262144`:

```bash
docker version
docker compose version
cat /proc/sys/vm/max_map_count
# Only if required, configure the host as administrator:
sudo sysctl -w vm.max_map_count=262144
```

For persistence across reboots, set that sysctl through your distribution's system configuration. Docker handles Python 3.15.2 / Node 22 inside images.

## First install

```bash
git clone https://github.com/YOUR_USERNAME/pdf-processor.git # substitute the repository address
cd pdf-processor
cp .env.example .env
chmod 600 .env
# Edit .env: set unique S3_SECRET_KEY; supply OPENROUTER_API_KEY only for real PDF jobs.
./scripts/setup.sh --no-prompt
```

Alternatively, extract the source ZIP and run the same commands from its `pdf-processor/` root. The setup script preserves existing `.env` and Docker named volumes, checks lockfiles, builds containers, initializes buckets/indexes and validates readiness. For interactive credential input, run `./scripts/setup.sh` without `--no-prompt`.

## Open the UI

- `http://localhost:3000` — React workspace and `/system` monitor.
- `http://localhost:8000` — FastAPI (API root prefix `/api/v1`).
- `http://localhost:9001` — local RustFS console; API endpoint is port 9000.

Ports bind to localhost. Access from another machine only via an authenticated tunnel/private network; do not publish the containers to the Internet. See [SECURITY.md](SECURITY.md).

## Free tests (no OpenRouter key)

```bash
./scripts/test_release.sh --offline
./scripts/check_figures.sh --offline
./scripts/check_regressions.sh
```

These tests use mocked provider responses and may rebuild/restart services. Run on an idle or disposable test deployment. **The default** `test_release.sh` mode makes paid requests; always specify `--offline` for free verification. See [CI](docs/CI_AND_TESTING.md).

## Routine operations and safe updates

```bash
docker compose ps
docker compose logs --tail=100 api worker frontend
# Stop, retaining named volumes:
docker compose down
# Rebuild or update in place without deleting volumes:
./scripts/setup.sh --no-prompt
```

**Never run `docker compose down -v` on a deployment whose data you want to keep.** It destroys Docker-managed volumes, including archived original PDFs, results, and search data. Back up with `scripts/backup_archive.sh` before significant changes, and test restoration separately. In-flight jobs may be affected by recreation of volatile Redis; schedule upgrades when idle. See [Operations](docs/OPERATIONS.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| Docker Compose not available | `docker compose version`, daemon permissions and installed Compose plugin |
| OpenSearch unhealthy | Host `vm.max_map_count`, memory availability, `docker compose logs opensearch` |
| Frontend fails to build | `docker compose build --no-cache frontend` and `frontend/tests/parser_contract.test.mjs`; the builder must copy `backend/app/schemas.py` |
| API or workers not ready | `docker compose ps`; inspect `api`, `worker`, `redis`, `rustfs` logs |
| PDF job cannot summarize | `.env` must provide an authorized OpenRouter key and available model; check sanitized diagnostics |
| Search seems stale | Inspect RustFS health and OpenSearch; rebuild projections with `docker compose exec -T api python -m app.catalogue_cli rebuild` |
| Missing figures in export | Verify run is complete or partial with committed extraction; inspect the figure manifest and relevant page output |

For environment settings see [Configuration](docs/CONFIGURATION.md). For recovery operations see [Operations](docs/OPERATIONS.md).
