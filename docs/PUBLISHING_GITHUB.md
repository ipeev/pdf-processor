# Publishing PDF Processor on GitHub

This is a **public source release**, not a declaration that the self-hosted application is ready for untrusted internet traffic. The service has no built-in login or per-user authorization. See [SECURITY.md](../SECURITY.md).

## Before pushing

- [ ] Ensure no `.env`, OpenRouter keys, user documents, generated model response logs, backups, server configuration, or private Git history are included. The bundled fixtures also require a final human redistribution/license review.
- [ ] Confirm the [MIT license](../LICENSE) attribution is the one you intend to publish.
- [ ] Verify manifest/lockfile metadata and the public documentation link and privacy checks:

```bash
python3 scripts/sync_release_version.py --check
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

From the repository root on `main`, review the source files and changes intended for the release:

```bash
git status --short
git diff --stat
git add -A
git diff --cached --stat
git diff --cached --check
VERSION="$(python3 scripts/sync_release_version.py --print)"
git commit -m "chore: release v${VERSION}"
git push origin main
```

Do not commit your release ZIP, local `.env`, development backups, Docker volumes, generated logs, or uploaded PDFs to the repository. A GitHub Release ZIP is attached to the **GitHub Release page** as an asset after CI is green; it is not added to the source tree.

- [ ] Wait for GitHub **CI / Source checks and frontend** and **CI / Complete offline Docker integration** to pass on the pushed commit.
- [ ] Check **CodeQL / Python** and **CodeQL / JavaScript/TypeScript**. CodeQL and Dependabot are configured; dependency updates are proposals and should not be merged without passing checks.
- [ ] Confirm CI runs with **no OpenRouter API key**, no paid inference, and only mocked/synthetic fixtures.
- [ ] Review the final documentation: [README](../README.md), [project overview](../project_overview.md), [architecture](ARCHITECTURE.md), [API](PERSISTENT_API.md), [full-text search](FULL_TEXT_SEARCH.md), [operations](OPERATIONS.md), and [roadmap](ROADMAP.md).

Only after the checks pass, tag the **same verified commit** and publish a GitHub Release:

```bash
VERSION="$(python3 scripts/sync_release_version.py --print)"
git tag -a "v${VERSION}" -m "PDF Processor v${VERSION}"
git push origin "v${VERSION}"
```

Attach the **complete version-matched source ZIP** named `pdf-processor-v${VERSION}-full.zip` to the GitHub Release (alongside GitHub’s automatically generated source archives). Confirm the ZIP was built from the same source snapshot as the verified tag, contains the MIT license and docs, and excludes private runtime files. Before announcing the release, confirm all required checks are green on the tag commit.

The release notes should describe the current capabilities and limitations: document extraction, asynchronous processing, figure/OCR pipeline and its imperfect crops, page cache/retries/progress, searchable archives, and free offline CI. Do not claim live-provider correctness or public internet deployment readiness based solely on offline tests.

The release must use the dependency versions recorded in `backend/uv.lock` and `frontend/package-lock.json`; do not resolve or update dependencies while assembling a source release. See [Dependency versions and verification](DEPENDENCY_UPGRADES.md).
