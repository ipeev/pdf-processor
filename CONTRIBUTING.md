# Contributing

Thanks for helping improve PDF Processor. This project is currently optimized for trusted localhost development, not public multiuser hosting.

## Before opening a pull request

1. Check for an existing issue and explain the behavior being changed. Include the parser ID and document state when relevant.
2. Create a small patch with tests. Keep backend/frontend parser contracts and locked dependencies consistent.
3. Run the **offline** checks in [CI and testing](docs/CI_AND_TESTING.md). Real-provider calls and spending are not required.
4. Do not include real customer PDFs, API keys, `.env`, unredacted server logs, or archive dumps in reports or fixtures. Use synthetic PDFs and masked diagnostics.
5. For user-visible changes, update the README/docs and add a test where practical.

## Code / environment

- Backend: Python 3.15.2 in Docker; FastAPI, `uv`, `backend/uv.lock`.
- Frontend: Node 22, React 19, TypeScript, Vite, `frontend/package-lock.json`.
- Services: Redis, RustFS, OpenSearch via Docker Compose.
- `npm ci` and `uv sync --locked` are deliberate reproducible installs. Lockfile changes should be reviewed together with updated manifests and verified by `scripts/check_release_candidate.py`.
- Do not modify the existing parser values (`pypdf`, `gemini`, `gpt5_mini`, `vision_figures`) without updating both backend and frontend validation/tests.

## Testing policy

For every PR, GitHub runs `CI` and `CodeQL`. The complete test gate uses fixtures and mocked provider interactions but real infrastructure services; no OpenRouter key or paid account should be needed. If a change cannot be tested offline, clearly describe the remaining manual verification in the PR.

For bug reports, include exact reproduction steps, expected and actual result, relevant log excerpt without secrets, and environment details. For suspected security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree your contribution can be distributed under the project's [MIT License](LICENSE). Do not submit proprietary documents, code or third-party assets without permission.
