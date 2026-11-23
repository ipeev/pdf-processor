#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# == 0 ]] || { echo 'Usage: ./scripts/validate_archive.sh' >&2; exit 2; }
docker compose exec -T api uv run --locked --extra test python -m app.operations_cli audit
