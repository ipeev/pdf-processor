#!/usr/bin/env bash
# Run browser-serving/proxy smoke checks from the uv-managed API container.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose exec -T api \
  uv run --locked --extra test python /app/scripts/check_frontend.py --base-url http://frontend
