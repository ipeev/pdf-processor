#!/usr/bin/env bash
# Demonstrate real Redis deduplication with mocked processing (zero AI charges).
set -euo pipefail
cd "$(dirname "$0")/.."
printf 'SHA-256 cache demonstration (real Redis, no OpenRouter calls)\n'
docker compose exec -T api uv run --locked --extra test \
  pytest -q -m redis_integration tests/test_phase4_cache.py -rs
