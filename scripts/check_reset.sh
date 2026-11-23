#!/usr/bin/env bash
# Explicitly destructive: clear all Compose volumes as well as volatile Redis.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" != "--confirm" ]]; then
  echo 'This command runs docker compose down -v: ALL project volume data will be deleted.' >&2
  echo 'Run ./scripts/check_reset.sh --confirm only if that is what you want.' >&2
  exit 2
fi
MARKER="reset-check-$$"
echo 'Checking Redis and the RustFS disk volume...'
docker compose exec -T redis redis-cli PING >/dev/null
docker compose exec -T redis redis-cli SET "$MARKER" should-disappear >/dev/null
OBJECT_ID="$(docker compose exec -T api python -m tests.phase3_storage_probe create | tr -d '\r\n')"
echo 'Deleting application containers and ALL named project volumes...'
docker compose down -v --remove-orphans
echo 'Starting empty application...'
docker compose up -d
for attempt in {1..30}; do
  if docker compose exec -T redis redis-cli PING >/dev/null 2>&1; then break; fi
  sleep 1
done
AFTER="$(docker compose exec -T redis redis-cli EXISTS "$MARKER" | tr -d '\r')"
if [[ "$AFTER" != '0' ]]; then
  echo 'FAIL: Redis retained data after the reset.' >&2
  exit 1
fi
READY=0
for attempt in {1..40}; do
  if ./scripts/check_workflow.sh --mode check >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
if [[ "$READY" != 1 ]]; then
  echo 'FAIL: application did not restart after down -v' >&2
  exit 1
fi
if docker compose exec -T api python -m tests.phase3_storage_probe exists "$OBJECT_ID" >/dev/null 2>&1; then
  echo 'FAIL: RustFS retained test object after deleting volumes.' >&2
  exit 1
fi
printf 'PASS: Redis and RustFS object volume were cleared by docker compose down -v.\n' 
