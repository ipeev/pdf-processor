#!/usr/bin/env bash
# Explicit local test of the eight Dependabot updates, without merging PRs.
# Run on an IDLE local directory where the current release has a built API image.
# No Git checkout is required. A local archive is created before any edits.
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  --yes) [[ $# -eq 1 ]] || exit 2; mode=upgrade ;;
  --resume) [[ $# -eq 1 ]] || exit 2; mode=resume ;;
  *)
    cat >&2 <<'USAGE'
Usage: ./scripts/upgrade_dependency_stack.sh --yes|--resume

--yes     Back up an unmodified baseline, prepare all eight upgrades,
          resolve real dependency locks, then run offline Docker tests.
--resume  Continue AFTER the manifest changes and BOTH locks were resolved.
          This DOES NOT prepare dependencies again or overwrite the backup.

Use only in an IDLE local development installation. No Git or OpenRouter calls.
The scripts do not delete named Docker volumes. Publication checks belong
on a clean release staging tree, not on your local directory containing .env.
USAGE
    exit 2 ;;
esac
if [[ "$(id -u)" == 0 ]]; then echo 'Run as a normal user, not root.' >&2; exit 2; fi
for cmd in python3 docker tar; do
  command -v "$cmd" >/dev/null || { echo "Missing command: $cmd" >&2; exit 1; }
done
if ! docker info >/dev/null 2>&1; then echo 'Docker daemon is not available.' >&2; exit 1; fi
if [[ "$mode" == 'upgrade' ]] && grep -Fq '"redis==8.1.0"' backend/pyproject.toml; then
  echo 'The eight dependency upgrades are already included in this source.' >&2
  echo 'Run ./scripts/test_release.sh --offline and ./scripts/check_regressions.sh to validate this build.' >&2
  echo 'Do not rerun the one-shot upgrade preparation against an upgraded release.' >&2
  exit 2
fi
# Fail before modifying files when the starting lock/manifest metadata is
# inconsistent; do not conceal source drift by resolving new locks.
python3 scripts/sync_release_version.py --check
python3 scripts/check_release_candidate.py
# Explicitly prevent paid calls even if .env contains a local provider key.
export OPENROUTER_API_KEY='' RUN_LIVE_TESTS=0
export OPENROUTER_MONITOR_ENABLED=true OBSERVABILITY_ENABLED=true
export DOCKER_TELEMETRY_ENABLED=false

run_offline_gates() {
  printf '\n=== Complete offline Docker integration (NO OpenRouter calls) ===\n'
  ./scripts/test_release.sh --offline
  printf '\n=== Focused figure/cache/search regressions (NO OpenRouter calls) ===\n'
  ./scripts/check_regressions.sh
}

if [[ "$mode" == 'resume' ]]; then
  # The first --yes invocation has already changed manifests and resolved
  # lockfiles. Re-running it would fail/re-apply a one-time migration.
  # Fail before touching Docker if the eight requested targets are incomplete.
  python3 - <<'PY_CHECK_UPGRADES'
from pathlib import Path
import json
import tomllib
root = Path('.')
project = tomllib.loads((root/'backend/pyproject.toml').read_text())['project']
pydeps = set(project['dependencies'])
testdeps = set(project['optional-dependencies']['test'])
frontend = json.loads((root/'frontend/package.json').read_text())
dockerfile = (root/'frontend/Dockerfile').read_text()
expected = {
    'redis==8.1.0': 'Python Redis client',
    'pypdf==7.1.1': 'PyPDF',
}
for requirement, label in expected.items():
    if requirement not in pydeps:
        raise SystemExit(f'FAIL: {label} not upgraded ({requirement} missing)')
if 'reportlab==5.1.0' not in testdeps:
    raise SystemExit('FAIL: ReportLab test dependency not upgraded')
for name, wanted in {
    'katex': '^0.19.1',
    '@vitejs/plugin-react': '^6.3.0',
    'vite': '^8.3.8',
}.items():
    deps = frontend['dependencies'] | frontend['devDependencies']
    if deps.get(name) != wanted:
        raise SystemExit(f'FAIL: {name} expected {wanted}, got {deps.get(name)}')
for image in ('node:26-bookworm-slim', 'nginx:1.31-alpine'):
    if f'FROM {image}' not in dockerfile:
        raise SystemExit(f'FAIL: Dockerfile does not use {image}')
print('PASS: all eight requested upgrade targets are present in the manifests.')
PY_CHECK_UPGRADES
  printf '%s\n' 'PASS: resolved locks verified; resuming local Docker tests.'
  printf '%s\n' 'NOTE: publication scan is intentionally deferred; local .env is private.'
  run_offline_gates
  printf '\nPASS: upgraded dependency stack passed the local offline gates. No Git operations were performed.\n'
  exit 0
fi

# Resolver comes from the prior working image, because building the NEW API
# before the new lockfile exists would correctly fail its --locked gate.
api_image="$(docker compose images -q api | head -n 1)"
if [[ -z "$api_image" ]]; then
  api_image="$(docker image inspect pdf-processor-api --format '{{.Id}}' 2>/dev/null || true)"
fi
if [[ -z "$api_image" ]]; then
  echo 'Build and verify the current baseline API image before upgrading.' >&2; exit 2
fi

# Keep a reversible source snapshot. It is local and deliberately excluded
# from source packages and Docker build contexts; it contains no .env secrets.
backup_dir='.local-upgrade-backups'
mkdir -p "$backup_dir"
backup="$PWD/$backup_dir/before-dependency-upgrades-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
tar -czf "$backup" -- \
  backend/pyproject.toml backend/uv.lock \
  frontend/package.json frontend/package-lock.json frontend/Dockerfile \
  frontend/tests/ui.test.mjs .github/workflows/ci.yml
printf 'Local source backup: %s\n' "$backup"
printf 'To restore source files: tar -xzf %q -C %q\n' "$backup" "$PWD"
trap 'echo "Upgrade failed. Source backup is: $backup" >&2' ERR

python3 scripts/prepare_dependency_upgrades.py --apply
printf '\n=== Resolve Python 3.15.2 dependency lock with old working uv image ===\n'
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache \
  --mount "type=bind,src=$(pwd)/backend,dst=/src" --workdir /src \
  --entrypoint uv "$api_image" lock --python /usr/local/bin/python3.15
printf '\n=== Resolve npm lock with Node 26; no postinstall execution ===\n'
node_image="$(awk '/^FROM node:/ {print $2; exit}' frontend/Dockerfile)"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e npm_config_cache=/tmp/npm-cache \
  --mount "type=bind,src=$(pwd)/frontend,dst=/workspace" --workdir /workspace \
  "$node_image" npm install --package-lock-only --ignore-scripts --no-audit --no-fund
printf '\n=== Preflight: reject incomplete upgrades and inconsistent locks ===\n'
python3 scripts/sync_release_version.py --check
python3 scripts/check_release_candidate.py
# Do NOT call check_publication.py against an installed development checkout.
# It intentionally rejects .env and other local-only private files. The strict
# publication scanner stays enabled in GitHub CI and final release packaging.
run_offline_gates
trap - ERR
printf '\nPASS: all dependency upgrades passed in the local development directory.\n'
printf 'Source backup (restore if needed): %s\n' "$backup"
printf 'No Git commands or Dependabot PR merges were performed.\n'
