#!/usr/bin/env bash
# Safe only when queue and archive are idle. Does not restart any container.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --cleanup ) ]]; then
  echo 'Usage: ./scripts/reconcile_archive.sh [--cleanup]' >&2; exit 2
fi
docker compose exec -T api uv run --locked --extra test python -m app.maintenance_cli "$@"
