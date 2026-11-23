#!/usr/bin/env bash
# Non-destructive to the running app: use an isolated, disposable Redis container,
# a separate RustFS bucket and private OpenSearch indexes. No provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
api_id="$(docker compose ps -q --status running api)"
[[ -n "$api_id" ]] || { echo 'API container must be running.' >&2; exit 1; }
network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$api_id" | grep '_default$' | head -1)"
[[ -n "$network" ]] || { echo 'Cannot find Docker Compose default network.' >&2; exit 1; }
token="$(od -An -N12 -tx1 /dev/urandom | tr -d '[:space:]')"
[[ "$token" =~ ^[0-9a-f]{24}$ ]] || exit 1
container="pdf-rc-redis-$token"
# Cleanup scoped test resources even after a failed test. Do not remove volumes.
cleanup() {
  exit_code=$?
  trap - EXIT
  if [[ "$started" == 1 ]]; then
    docker compose run --rm -T --no-deps -e "REDIS_URL=redis://$container:6379/0" \
      -e OPENROUTER_API_KEY= api python -m tests.rc_redis_restart cleanup "$token" || \
      echo 'WARNING: isolated test bucket cleanup failed; inspect the rc.1 bucket manually.' >&2
    docker rm -f "$container" >/dev/null 2>&1 || true
  fi
  exit "$exit_code"
}
started=0
trap cleanup EXIT
docker run --rm -d --name "$container" --network "$network" --memory 256m \
  --user 10001:10001 --read-only --tmpfs /data:size=67108864,mode=1777 \
  --tmpfs /tmp:size=33554432,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges redis:7.4-alpine \
  redis-server --appendonly no --save '' >/dev/null
started=1
for ((attempt=0;attempt<40;attempt++)); do
  if docker exec "$container" redis-cli PING 2>/dev/null | grep -qx PONG; then break; fi
  sleep 1
done
docker exec "$container" redis-cli PING | grep -qx PONG
printf '\n=== RC.1: real Redis container restart with isolated active jobs ===\n'
docker compose run --rm -T --no-deps -e "REDIS_URL=redis://$container:6379/0" \
  -e OPENROUTER_API_KEY= api python -m tests.rc_redis_restart before "$token"
before_epoch="$(docker exec "$container" redis-cli INFO server | tr -d '\r' | sed -n 's/^run_id://p')"
[[ -n "$before_epoch" ]] || { echo 'No Redis server epoch before restart.' >&2; exit 1; }
docker restart "$container" >/dev/null
for ((attempt=0;attempt<40;attempt++)); do
  if docker exec "$container" redis-cli PING 2>/dev/null | grep -qx PONG; then break; fi
  sleep 1
done
after_epoch="$(docker exec "$container" redis-cli INFO server | tr -d '\r' | sed -n 's/^run_id://p')"
[[ -n "$after_epoch" && "$before_epoch" != "$after_epoch" ]] || { echo 'Real Redis server epoch did not change.' >&2; exit 1; }
docker compose run --rm -T --no-deps -e "REDIS_URL=redis://$container:6379/0" \
  -e OPENROUTER_API_KEY= api python -m tests.rc_redis_restart after "$token"
printf 'PASS: actual Redis restart recovery verified without touching production Redis or archive.\n'
