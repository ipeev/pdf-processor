# Operations, recovery and monitoring — PDF Processor

All commands run from the repository root on a **trusted, idle Linux deployment**. This project is single-host/localhost only. Operations may affect Redis or maintenance locks; test backup/restore on disposable infrastructure first.

## Health and runtime inspection

```bash
docker compose ps
docker compose logs --tail=100 api worker redis rustfs opensearch frontend
./scripts/check_workflow.sh --mode check
./scripts/check_opensearch.sh --check
```

`/system` displays Redis/worker/temporary-storage/archive measurements, search health, and optional Docker/OpenRouter monitoring values. Unknown metrics are shown as unavailable rather than fabricated zeros. The observability stack (Grafana, Loki, Tempo, Alloy) is **not included**; optional OpenTelemetry exports require an independently configured endpoint.

## Storage, retention and safe restarts

- The permanent `S3_ARCHIVE_BUCKET` (by default `pdf-documents`) contains authoritative originals and run assets; it is not subject to the short temporary upload lifecycle.
- Redis deliberately runs without AOF or RDB persistence. Its job/monitor/cache state can disappear on restart; durable source/result records remain in RustFS.
- OpenSearch holds rebuildable metadata **and extracted full-text passage indexes** and runs without a public port. Its named volume can be recreated from the archive if authoritative data remains intact.
- All Docker services use bounded JSON-file log rotation (20 MB x 5 files per container).
- `docker compose down` retains named volumes. **`docker compose down -v` permanently removes named volumes**, including PDF originals, results, and image data. Never use `-v` for a routine restart.

## Backup and restoration

```bash
# Backup committed archive objects to a new, owner-readable ZIP path:
./scripts/backup_archive.sh ./archive-backup.zip

# RESTORE ONLY INTO AN EMPTY TARGET after verifying the backup file:
./scripts/restore_archive.sh ./archive-backup.zip --confirm-empty

# Refresh OpenSearch catalogue projections from canonical archive:
./scripts/rebuild_archive_index.sh --confirm
```

The backup wrapper refuses to overwrite an existing destination and writes with restrictive permissions. Backups can contain **original uploaded PDFs and sensitive content**; encrypt and store them outside the public repository. Restore rejects nonempty target archives and should be rehearsed on a separate disposable installation. Catalogue rebuild requires an idle queue and recreates only derived indexes; it does not replace missing RustFS source content.

## Index existing documents for full-text search

```bash
# After upgrading an existing archive, or when search needs repair:
docker compose exec -T api python -m app.search_cli init
# Run while the processing queue is idle; uses only archived RustFS text:
docker compose exec -T api python -m app.search_cli rebuild
```

The rebuild clears and recreates **only** `pdf-passages-v1`, then reindexes available completed and partial extractions. It does **not** call OpenRouter, replace the original PDFs, or delete the archive. New terminal processing runs are indexed automatically on a best-effort basis. If OpenSearch is unavailable or results appear stale after a crash, rerun the command on an idle system. See [Full-text search](FULL_TEXT_SEARCH.md).

## Archive reconciliation and deletion

```bash
# Inspect/reconcile persistent metadata, no paid OpenRouter calls:
./scripts/reconcile_archive.sh

# Optional cleanup of specifically identified unreachable artifacts:
./scripts/reconcile_archive.sh --cleanup
```

Deletion is available through the app/API with correct run state, archive maintenance coordination, and revision preconditions. Deleting a document removes its original and all runs. Deleting a single run retains the canonical original and other run-owned copies. Reconciliation can restore consistent projections after interrupted operations, but must not be treated as a backup replacement.

## Failure and recovery guidance

| Symptom | First response |
|---|---|
| RustFS unhealthy | Restore service access; do not delete the volume; inspect logs and backup status |
| Missing archive object | Treat as data-integrity issue; restore from a verified backup rather than fabricating an empty result |
| Full-text search empty after upgrade | Run `python -m app.search_cli rebuild` in the API container (idle queue; no paid calls) |
| Full-text search stale/503 | Check OpenSearch logs, then perform an idle search rebuild; the RustFS archive remains canonical |
| History or Library unavailable | Check OpenSearch and RustFS; rebuild projections on idle system |
| Redis restarted during work | Check in-flight/unknown outcomes and reconciled durable run state; do not blindly resubmit paid calls |
| Summary failed but pages exist | Use the explicit summary-only retry for `partial` runs |
| Figures present in metadata but image inaccessible | Verify run-owned PNG integrity, storage and API; inspect sanitized errors |
| `VISION_RESPONSE_INVALID` on image OCR | Inspect [vision response diagnostics](VISION_RESPONSE_DIAGNOSTICS.md); temporarily log full rejected replies on a trusted host |
| Worker memory pressure | Reduce `WORKER_THREADS`, scale or batch size; monitor Docker limits |

## Free verification after maintenance

```bash
./scripts/test_release.sh --offline
./scripts/check_figures.sh --offline
./scripts/check_regressions.sh
```

These tests can restart and modify isolated service test state; use a disposable/idle stack. Do **not** accidentally run `test_release.sh` without `--offline`, as the default invokes paid tests. See [CI and testing](CI_AND_TESTING.md).

## Release upgrades and stale containers

Each application service (`api`, `worker`, `frontend`) carries a release label in
`docker-compose.yml`. Compose uses changes in service configuration to recreate
containers when the release changes. `scripts/setup.sh` additionally compares
the running API and every worker project version against `backend/pyproject.toml`
before continuing with acceptance tests.

If an upgrade was interrupted or a container predates the source checkout,
wait until active document processing completes, then run:

```sh
docker compose up -d --build --force-recreate api worker frontend
```

If the version check still reports an old image after a forced container
recreation, rebuild the application images without BuildKit cache and recreate
them. Do this only once active jobs have completed:

```sh
docker compose build --no-cache api worker frontend
docker compose up -d --force-recreate api worker frontend
```

These commands do not remove the named RustFS/OpenSearch volumes. Do not use
`docker compose down -v`; it deletes the archived data volumes.
