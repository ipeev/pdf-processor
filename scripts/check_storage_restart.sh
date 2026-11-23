#!/usr/bin/env bash
# Real RustFS restart persistence check. Uses only disposable probe objects.
# Optional Redis reset is destructive to live jobs/cache: explicit opt-in only.
set -euo pipefail
cd "$(dirname "$0")/.."
service=rustfs
if [[ "$#" -ne 0 ]]; then
  if [[ "$#" -eq 2 && "$1" == --restart-redis && "$2" == --confirm-empty-development ]]; then
    service=redis
    echo 'WARNING: restarting Redis clears volatile job state and cached results. Only use on an empty development deployment.' >&2
  else
    echo 'Usage: ./scripts/check_storage_restart.sh [--restart-redis --confirm-empty-development]' >&2; exit 2
  fi
fi
uid=''
# Canonical UUIDs include four hyphens; use the same validator for both probes.
uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
archive_token="$(cat /proc/sys/kernel/random/uuid)"
[[ "$archive_token" =~ $uuid_pattern ]] || { echo 'Invalid archive probe UUID' >&2; exit 1; }
archive_probe() { docker compose exec -T api python -m tests.archive_probe "$@"; }
temporary_probe() { docker compose exec -T api python -m tests.phase3_storage_probe "$@"; }
cleanup() {
  if [[ -n "$uid" ]]; then temporary_probe cleanup "$uid" >/dev/null 2>&1 || echo 'WARNING: temp probe cleanup failed' >&2; fi
  archive_probe cleanup "$archive_token" >/dev/null 2>&1 || echo 'WARNING: archive probe cleanup failed' >&2
}
trap cleanup EXIT
printf 'Creating isolated temporary and archive objects (two runs)...\n'
candidate="$(temporary_probe create | tr -d '\r\n')"
[[ "$candidate" =~ $uuid_pattern ]] || { echo 'Invalid S3 probe UUID' >&2; exit 1; }
uid="$candidate"
archive_probe create "$archive_token"
printf 'Restarting %s service without deleting RustFS volume...\n' "$service"
docker compose restart "$service"
ready=0
for ((i=0;i<40;i++)); do
  if temporary_probe exists "$uid" >/dev/null 2>&1 && archive_probe check >/dev/null 2>&1; then
    if [[ "$service" == redis ]] && [[ "$(docker compose exec -T redis redis-cli PING 2>/dev/null | tr -d '\r')" != PONG ]]; then
      sleep 2; continue
    fi
    ready=1; break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo 'FAIL: storage or restarted service is not ready.' >&2
  docker compose logs --tail=50 "$service" >&2 || true
  exit 1
fi
temporary_probe verify "$uid"
archive_probe verify "$archive_token"
# Cleanup failure is a release failure, not a false pass.
temporary_probe cleanup "$uid"
uid=''
archive_probe cleanup "$archive_token"
trap - EXIT
printf 'PASS: temporary PDF, canonical original, two independent run records and all artifacts survived %s restart.\n' "$service"
