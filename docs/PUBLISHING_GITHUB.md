# Public GitHub publication checklist — PDF Processor 0.12.3

This archive is prepared as a **new source repository**. It is not automatically published, and the first remote GitHub Actions results have not yet been observed.

## Before creating the repository

- [ ] Review the [MIT License](../LICENSE) and change the generic copyright holder to the correct legal attribution if required.
- [ ] Confirm rights to redistribute *all code and bundled test PDFs*, benchmark fixtures, diagrams, and third-party materials. A generated fixture is not automatically licensed solely because it is in the archive.
- [ ] Run `python3 scripts/check_publication.py` and `python3 scripts/check_release_candidate.py` and inspect the staged file list manually. Automated scans cannot prove that every source or PDF is free from private information.
- [ ] Verify `.env`, user PDFs, provider keys, tokens, backups, logs, and test results are not tracked. Do not import unrelated or private Git history.
- [ ] Read [security limits](../SECURITY.md): the application's localhost deployment is not safe for public HTTP exposure.

## Create and push a public repository

From the extracted `pdf-processor` directory:

```bash
git init -b main
git add .
git status --short
git diff --cached --stat
# Inspect the actual staged diff as well.
git commit -m "Initial public release v0.12.3"
```

Create a public repository named `pdf-processor` through GitHub, then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/pdf-processor.git
git push -u origin main
```

Or use authenticated `gh repo create pdf-processor --public --source=. --remote=origin --push`. Replace `YOUR_USERNAME` with the actual account; no private server URL belongs in source documentation.

## Verify free CI and publish the release

- [ ] Check **Actions → CI**: `source-and-frontend` and `offline-integration` must pass.
- [ ] Check **Actions → CodeQL**: Python and JavaScript/TypeScript analyses must complete; do not enable duplicate scanning setups.
- [ ] Enable Dependabot security alerts/updates; review any lockfile changes before merging.
- [ ] Protect `main` using a branch ruleset and require passing CI. Use read-only defaults for `GITHUB_TOKEN`.
- [ ] Do not add an OpenRouter key to Actions; no paid inference is required to test this repository.
- [ ] Publish GitHub release and tag `v0.12.3` after passing checks. Release notes should describe the **current capabilities and known limitations** rather than private development events.

For the current feature scope, see [project overview](../project_overview.md); for follow-up ideas, see [roadmap](ROADMAP.md).
