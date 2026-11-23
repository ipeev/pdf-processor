#!/usr/bin/env bash
# Generate and retain frontend/package-lock.json with Node 22 inside Docker.
# Requires network access to npm registry only on first lock generation or upgrade.
set -euo pipefail
cd "$(dirname "$0")/.."
# Never give a bind-mounted project directory to a root-run container.
if [[ "$(id -u)" -eq 0 ]]; then
  echo 'ERROR: run this script as your normal user, not sudo/root.' >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo 'Docker is required to resolve frontend dependencies. See QUICKSTART_LINUX.md.' >&2
  exit 1
fi
printf '%s\n' '=== Resolving frontend/package-lock.json with npm (Node 22 in Docker) ==='
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e npm_config_cache=/tmp/npm-cache \
  --mount "type=bind,src=$(pwd)/frontend,dst=/workspace" \
  --workdir /workspace \
  node:22-bookworm-slim npm install --package-lock-only --ignore-scripts --no-audit --no-fund
if [[ ! -s frontend/package-lock.json ]]; then
  echo 'Frontend lockfile generation failed.' >&2
  exit 1
fi
printf 'Lockfile written: frontend/package-lock.json (%s bytes)\n' "$(wc -c < frontend/package-lock.json)"
printf '%s\n' 'Keep and commit this lockfile so Docker builds can use npm ci.'
