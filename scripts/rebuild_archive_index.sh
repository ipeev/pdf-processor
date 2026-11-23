#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# == 1 && "$1" == --confirm ]] || { echo 'Usage: ./scripts/rebuild_archive_index.sh --confirm (idle queue; recreates only the two catalogue indexes)' >&2; exit 2; }
docker compose exec -T api uv run --locked --extra test python -m app.operations_cli rebuild
