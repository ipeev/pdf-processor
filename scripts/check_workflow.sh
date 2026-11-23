#!/usr/bin/env bash
# Run the stdlib-only Python end-to-end verifier inside the uv-managed API container.
# No host Python, jq, curl, or copied UUIDs required.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose exec -T api \
  uv run --locked --extra test python /app/scripts/check_workflow.py \
  --base-url "${API_BASE_URL:-http://127.0.0.1:8000/api/v1}" "$@"
