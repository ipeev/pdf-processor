#!/usr/bin/env bash
# Compatibility shortcut: one PDF, no manual UUIDs, automatically polls and validates.
# Bundled samples run inside the uv-managed container. Arbitrary host files use
# Python's standard library on Linux (no additional pip dependencies).
set -euo pipefail
cd "$(dirname "$0")/.."
PDF="${1:-samples/basic_text.pdf}"
PARSER="${2:-pypdf}"
if [[ "$PDF" == samples/* && -f "$PDF" ]]; then
  exec ./scripts/check_workflow.sh --mode custom --file "$PDF" --parser "$PARSER" --show-results
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Custom host paths need Python 3. Alternatively use a bundled sample via ./scripts/check_workflow.sh.' >&2
  exit 1
fi
exec python3 scripts/check_workflow.py --mode custom --file "$PDF" --parser "$PARSER" --show-results \
  --base-url "${API_BASE_URL:-http://127.0.0.1:8000/api/v1}"
