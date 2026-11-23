#!/usr/bin/env bash
# Show RAM usage and temporary-retention policy without modifying data.
set -euo pipefail
cd "$(dirname "$0")/.."
printf '=== Redis memory ===\n'
docker compose exec -T redis redis-cli INFO memory | tr -d '\r' | \
  grep -E '^(used_memory_human|maxmemory_human|maxmemory_policy|used_memory_dataset_perc):' || true
printf '\n=== Persistence ===\n'
docker compose exec -T redis redis-cli INFO persistence | tr -d '\r' | \
  grep -E '^(aof_enabled|rdb_last_save_time):' || true
printf '\n=== Keys in current database ===\n'
docker compose exec -T redis redis-cli DBSIZE
printf '\n=== Pending jobs ===\n'
STREAM_KEY="$(docker compose exec -T worker sh -c 'printf "%s" "${STREAM_KEY:-documents:jobs}"')"
GROUP="$(docker compose exec -T worker sh -c 'printf "%s" "${CONSUMER_GROUP:-pdf-workers}"')"
docker compose exec -T redis redis-cli XPENDING "$STREAM_KEY" "$GROUP" || true
