# Publishing PDF Processor v0.12.7 on GitHub

This is a **public source release**, not a declaration that the self-hosted application is ready for untrusted internet traffic. The service has no built-in login or per-user authorization. See [SECURITY.md](../SECURITY.md).

## Before pushing

- [ ] Ensure no `.env`, OpenRouter keys, user documents, generated model response logs, backups, server configuration, or private Git history are included. The bundled fixtures also require a final human redistribution/license review.
- [ ] Confirm the [MIT license](../LICENSE) attribution is the one you intend to publish.
- [ ] Verify manifest/lockfile metadata and the public documentation link and privacy checks:

```bash
python3 scripts/check_release_candidate.py
python3 scripts/check_publication.py
python3 -m compileall -q backend/app scripts
find scripts -name '*.sh' -print0 | xargs -0 -r bash -n
```

- [ ] Run the full **offline** integration suite in an idle or disposable Linux Docker environment (not while processing paid documents):

```bash
./scripts/test_release.sh --offline
./scripts/check_regressions.sh
```

Do not run `./scripts/test_release.sh` without `--offline` as the default mode can call paid OpenRouter models. **Never** use `docker compose down -v` if you need to preserve existing RustFS archives.

## Push and verify

From your existing `main` working tree, replace only the reviewed release files, then inspect the staged changes:

```bash
git status --short
git diff --stat
git add -A
git diff --cached --stat
git diff --cached --check
git commit -m "chore: document and validate v0.12.7 release"
git push origin main
```

- [ ] Wait for GitHub **CI / Source checks and frontend** and **CI / Complete offline Docker integration** to pass on the pushed commit.
- [ ] Check **CodeQL / Python** and **CodeQL / JavaScript/TypeScript**. CodeQL and Dependabot are configured; dependency updates are proposals and should not be merged without passing checks.
- [ ] Confirm CI runs with **no OpenRouter API key**, no paid inference, and only mocked/synthetic fixtures.
- [ ] Review the final documentation: [README](../README.md), [project overview](../project_overview.md), [architecture](ARCHITECTURE.md), [API](PERSISTENT_API.md), [full-text search](FULL_TEXT_SEARCH.md), [operations](OPERATIONS.md), and [roadmap](ROADMAP.md).

Only after the checks pass, tag the **same verified commit** and publish a GitHub Release:

```bash
git tag -a v0.12.7 -m "PDF Processor v0.12.7"
git push origin v0.12.7
```

The release notes should describe the current capabilities and limitations: document extraction, asynchronous processing, figure/OCR pipeline and its imperfect crops, page cache/retries/progress, searchable archives, and free offline CI. Do not claim live-provider correctness or public internet deployment readiness based solely on offline tests.
