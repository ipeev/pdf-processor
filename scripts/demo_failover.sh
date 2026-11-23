#!/usr/bin/env bash
# Deterministic two-consumer takeover against real Docker Redis; no paid models.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/setup.sh --no-prompt
printf '\n=== Two running worker containers ===\n'
workers="$(docker compose ps -q --status running worker | sed '/^$/d' | wc -l | tr -d ' ')"
if [[ "$workers" -lt 2 ]]; then
  echo "FAIL: expected at least two running worker containers; found $workers" >&2
  exit 1
fi
printf 'PASS: %s workers running\n' "$workers"
printf '\n=== Real Redis worker-crash/takeover tests (no OpenRouter) ===\n'
docker compose exec -T api uv run --locked --extra test pytest -q -m redis_integration \
  tests/test_phase2_failover.py -rs
printf '\nPASS: worker takeover, fencing, and heartbeat integration tests.\n'
