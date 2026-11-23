#!/usr/bin/env bash
set -euo pipefail
[[ $# == 2 && "$2" == --confirm-empty ]] || { echo 'Usage: ./scripts/restore_archive.sh INPUT.zip --confirm-empty (fresh installation only)' >&2; exit 2; }
input="$(realpath -- "$1")"
[[ -f "$input" ]] || { echo 'Backup file not found.' >&2; exit 2; }
cd "$(dirname "$0")/.."
docker compose exec -T api uv run --locked --extra test python -m app.operations_cli restore < "$input"
