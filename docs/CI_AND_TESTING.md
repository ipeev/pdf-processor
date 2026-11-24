# CI, offline tests, and acceptance — PDF Processor 0.12.8

All required GitHub Actions run **without `OPENROUTER_API_KEY`**, without external inference requests, and without paid test accounts. AI output is simulated using deterministic fixtures and mocked provider responses. Real Redis, RustFS and OpenSearch integration coverage runs inside Docker.

## Workflows

| Workflow | Trigger | Coverage | Paid AI |
|---|---|---|---|
| **CI / source-and-frontend** | push / pull request / manual | Publication/privacy pattern checks; Python and npm lockfile consistency; Python compilation; shell syntax; Compose configuration; Node tests; TypeScript/Vite production build | No |
| **CI / offline-integration** | push / pull request / manual | Docker images, backend tests, real service API/queue/archive/search integration, recovery/failover/figures, and uncached frontend regression build | No |
| **CodeQL** | push / pull request / weekly / manual | Python and JavaScript/TypeScript static security analysis | No |
| **Dependabot** | weekly | Reviewable dependency/Action/Docker image update PRs | No |

CI uses read-only repository permissions where possible; CodeQL has permission to upload code-scanning results. The Docker job explicitly creates `.env` from synthetic `.env.example`, clears the provider key, sets `RUN_LIVE_TESTS=0`, and calls **only offline** verification commands. It must never take paid inference credentials from GitHub secrets or pull-request submissions.

## Run locally

```bash
# Standard library checks (Python >=3.11):
python3 scripts/check_release_candidate.py
python3 scripts/check_publication.py
python3 -m compileall -q backend/app scripts

# Requires Docker daemon and an idle/disposable application environment:
./scripts/test_release.sh --offline
./scripts/check_figures.sh --offline
./scripts/check_regressions.sh
```

`check_release_candidate.py` validates lockfiles **and** public documentation and CI references in the complete source checkout. The full release gate runs that check before starting Docker; inside the minimal Python image, it runs `check_release_candidate.py --runtime-only` to validate the same lockfile/manifests without expecting README, docs or GitHub Actions files in the runtime image. CI also runs the complete-source check independently in the source job. A missing source document still fails the release gate.

`test_release.sh --offline` is the comprehensive release gate. `check_figures.sh --offline` exercises the figure pipeline against real infrastructure and saved model responses. `check_regressions.sh` forces an uncached frontend Docker build and runs focused caption/figure Python tests. This catches incorrect `COPY` paths or frontend/backend parser-contract drift. Offline suites include extensive synthetic unit, API, security, cache and archive-recovery cases.

The scripts can rebuild/restart Docker services and modify test namespaces; do not run them during important ongoing work. Running `./scripts/test_release.sh` **without `--offline`** invokes paid OpenRouter calls. The provider key is deliberately absent in GitHub CI.

The current suite includes `backend/tests/test_fulltext_search.py` (offline unit contracts), `backend/tests/test_fulltext_search_integration.py` (real OpenSearch BM25, page selection, document collapse), and `frontend/tests/search.test.mjs` (routing, validation, safe text highlighting). No OpenRouter key is used.

For frontend-only development:

```bash
cd frontend
npm ci --no-audit --no-fund
npm test
npm run build
```

The backend production image uses Python 3.15.2 and `uv sync --locked`; frontend uses Node 22 and `npm ci`. Locked Python and npm dependencies are committed and checked on each CI run.

**Regression gates:** besides the full backend test suite, the focused regression script reruns tests for vision-page caching/retries, full-response diagnostics, page progress, and the cache test doubles using the keyword-only `on_progress` callback. This catches interface drift without real OpenRouter calls.

## What passing CI means—and does not mean

**Covered:** source/lock consistency, frontend/backend parser contract, image/Markdown API and ZIP integrity, mocked provider boundaries, queue/ownership and cache/recovery invariants, real storage/search interactions, content safety, real BM25 passage search/collapse with synthetic texts, and container build paths.

**Not covered:** live OpenRouter availability, provider-model compatibility, provider billing, visual quality on arbitrary PDFs, exact OCR completeness, and safe exposure to the public internet. Image crops and caption alignment remain best effort; no test should pretend otherwise.

An optional *explicitly paid* manual check is:

```bash
./scripts/check_figures.sh --paid-live
```

This sends test PDF pages to a configured model and may incur cost. It is intentionally outside GitHub Actions. For publication, the first remote CI/CodeQL run must still pass; validated YAML and local test results are not a substitute for actual runner execution.
