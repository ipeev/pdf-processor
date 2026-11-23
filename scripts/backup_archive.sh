#!/usr/bin/env bash
set -euo pipefail
[[ $# == 1 ]] || { echo 'Usage: ./scripts/backup_archive.sh OUTPUT.zip' >&2; exit 2; }
# Resolve relative path against caller's directory, never the service container.
output="$(realpath -m -- "$1")"
[[ ! -e "$output" && ! -L "$output" ]] || { echo 'Output already exists; refusing overwrite.' >&2; exit 2; }
umask 077
tmp="$(mktemp -- "$(dirname "$output")/.archive-backup.XXXXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
cd "$(dirname "$0")/.."
docker compose exec -T api uv run --locked --extra test python -m app.operations_cli backup > "$tmp"
# Atomic no-clobber publication on the same filesystem.
ln -T -- "$tmp" "$output"
chmod 600 -- "$output"
printf 'Backup written: %s\n' "$output"
