# Dependency versions and verification

PDF Processor ships pinned Python and frontend dependencies in `backend/uv.lock` and `frontend/package-lock.json`. The application builds with Python 3.15.2 and Node.js 26 inside Docker; a local Python or Node installation is not required for ordinary deployment.

## Shipped dependency stack

| Component | Locked version or image |
|---|---|
| Python Redis client | 8.1.0 |
| PyPDF | 7.1.1 |
| ReportLab (test dependency) | 5.1.0 |
| Vite | 8.3.8 |
| `@vitejs/plugin-react` | 6.3.0 |
| KaTeX | 0.19.1 |
| Node.js frontend build image | `node:26-bookworm-slim` |
| Nginx frontend runtime image | `nginx:1.31-alpine` |
| Redis server image | `redis:7.4-alpine` |
| OpenSearch server image | `opensearchproject/opensearch:3.10.0` |
| RustFS server image | `rustfs/rustfs:1.0.3` |

The Python Redis client and Redis server are separately versioned. `@types/node` also has an independent lockfile version; the frontend build image version is not inferred from the TypeScript type definitions. Consult `backend/uv.lock`, `frontend/package-lock.json`, and `docker-compose.yml` for the complete transitive dependency graph and service configuration.

## Reproducible installation and verification

The source distribution includes both lockfiles. Docker uses `uv sync --locked` for Python dependencies and `npm ci` for the frontend build. **Installation does not require regenerating the lockfiles or applying dependency upgrade scripts.**

From the repository root on an idle Docker test installation:

```bash
python3 scripts/sync_release_version.py --check
python3 scripts/check_release_candidate.py
./scripts/test_release.sh --offline
./scripts/check_regressions.sh
```

The offline suite exercises the application with real Redis, RustFS, and OpenSearch services, runs backend tests and frontend build/tests, and uses mocked or saved AI replies. It does not issue paid OpenRouter calls and cannot certify live model behavior or OCR/figure quality on arbitrary documents. Docker builds may recreate application containers; run only on idle or disposable infrastructure. **Do not run `docker compose down -v` on a deployment containing archives you wish to keep.**

## Dependency maintenance

Dependency changes require matching updates to the manifest and its resolver-generated lockfile. Do not hand-edit or invent package integrity hashes. Review the resulting dependency graph, test the production Docker images, and complete the offline integration checks before publishing a change. Keep major upgrades isolated when that makes compatibility failures easier to identify.

The canonical project version is the `version` field in `backend/pyproject.toml`. Package managers and Docker Compose require synchronized copies:

```bash
python3 scripts/sync_release_version.py --write
python3 scripts/sync_release_version.py --check
```

The synchronization tool updates only project version metadata; it does not resolve dependencies. Run `python3 scripts/check_publication.py` on a **clean public source tree** without `.env`, generated logs, backups, or private runtime files. A working deployment can legitimately contain a private `.env`, which must not be included in the release archive.
