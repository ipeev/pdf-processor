#!/usr/bin/env bash
# Optional real Docker container crash demo using mock summarization. No paid calls.
# Requires an already-running project stack; isolated Stream, no production jobs.
set -euo pipefail
cd "$(dirname "$0")/.."
prefix="phase2demo:$(date +%s)-$RANDOM-$RANDOM"
name1="pdf-phase2-stalled-${RANDOM}"
name2="pdf-phase2-rescuer-${RANDOM}"
doc_id=''
cleanup() {
  docker rm -f "$name1" "$name2" >/dev/null 2>&1 || true
  if [[ -n "$doc_id" ]]; then
    docker compose exec -T -e DEMO_PREFIX="$prefix" api \
      python -m tests.phase2_container_worker cleanup "$doc_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if ! docker compose ps -q --status running redis | grep -q .; then
  echo 'Redis must already be running. Run ./scripts/setup.sh first.' >&2
  exit 1
fi
printf 'Creating isolated demo PDF job (no OpenRouter calls)...\n'
doc_id="$(docker compose exec -T -e DEMO_PREFIX="$prefix" api \
  python -m tests.phase2_container_worker create | tr -d '\r\n')"
[[ "$doc_id" =~ ^[a-f0-9-]{36}$ ]] || { echo 'Could not create demo document' >&2; exit 1; }

printf 'Starting first temporary worker container...\n'
COMPOSE_IGNORE_ORPHANS=true docker compose run -d --no-deps --name "$name1" \
  -e DEMO_PREFIX="$prefix" -e DEMO_MODE=stall \
  worker python -m tests.phase2_container_worker work >/dev/null
# The demo intentionally kills this one-off container, not a normal worker.
docker update --restart=no "$name1" >/dev/null
started=0
for ((i=0;i<60;i++)); do
  marker="$(docker compose exec -T redis redis-cli --raw GET "$prefix:started" | tr -d '\r')"
  if [[ "$marker" == 1 ]]; then started=1; break; fi
  sleep .5
done
if [[ "$started" != 1 ]]; then
  docker logs --tail 50 "$name1" >&2 || true
  echo 'FAIL: first worker did not reach the blocked summary stage.' >&2
  exit 1
fi
printf 'PASS: Worker A extracted pages and is blocked in mock summarization.\n'
printf 'Killing worker A container with SIGKILL...\n'
docker kill "$name1" >/dev/null

printf 'Starting independent worker B to claim the pending job...\n'
COMPOSE_IGNORE_ORPHANS=true docker compose run -d --no-deps --name "$name2" \
  -e DEMO_PREFIX="$prefix" -e DEMO_MODE=survive \
  -e WORKER_HEARTBEAT_SECONDS=1 -e WORKER_CLAIM_IDLE_SECONDS=5 \
  worker python -m tests.phase2_container_worker work >/dev/null

status=''
for ((i=0;i<60;i++)); do
  status="$(docker compose exec -T redis redis-cli --raw HGET \
    "$prefix:doc:$doc_id:meta" status | tr -d '\r')"
  if [[ "$status" == completed ]]; then break; fi
  if [[ "$status" == failed || "$status" == partial ]]; then break; fi
  sleep .5
done
if [[ "$status" != completed ]]; then
  docker logs --tail 50 "$name2" >&2 || true
  echo "FAIL: recovering worker did not complete the job (status=$status)." >&2
  exit 1
fi
summary="$(docker compose exec -T redis redis-cli --raw GET \
  "$prefix:doc:$doc_id:summary" | tr -d '\r')"
if [[ "$summary" != 'Recovered by the second Docker worker (mock summarizer).' ]]; then
  echo 'FAIL: wrong recovered summary' >&2
  exit 1
fi
if ! docker compose exec -T -e DEMO_PREFIX="$prefix" api \
  python -m tests.phase2_container_worker exists "$doc_id"; then
  echo 'FAIL: source PDF was not deleted after acknowledgment' >&2
  exit 1
fi
printf 'PASS: Worker B claimed the abandoned job, reused extraction and completed it.\n'
printf 'PASS: Original S3 PDF object removed after ACK. No OpenRouter calls.\n'
