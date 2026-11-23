#!/usr/bin/env bash
# Verify Docker Engine log rotation for every PDF Processor service.
# Does not delete data or modify the Docker daemon.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose config --quiet
printf 'Verifying Docker log rotation for all PDF Processor containers...\n'

for service in frontend redis rustfs-permissions rustfs opensearch api worker; do
  ids="$(docker compose ps -aq "$service")"
  if [[ -z "$ids" ]]; then
    if [[ "$service" == rustfs-permissions ]]; then
      printf 'INFO: rustfs-permissions one-shot container absent; Compose policy still covers it.\n'
      continue
    fi
    echo "FAIL: no container found for $service" >&2
    exit 1
  fi

  count=0
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    count=$((count + 1))
    driver="$(docker inspect --format '{{.HostConfig.LogConfig.Type}}' "$id")"
    max_size="$(docker inspect --format '{{index .HostConfig.LogConfig.Config "max-size"}}' "$id")"
    max_file="$(docker inspect --format '{{index .HostConfig.LogConfig.Config "max-file"}}' "$id")"
    if [[ "$driver" != json-file || "$max_size" != 20m || "$max_file" != 5 ]]; then
      echo "FAIL: $service ($id): driver=$driver max-size=$max_size max-file=$max_file; expected json-file/20m/5" >&2
      echo 'Recreate containers when idle to apply the new Compose logging settings.' >&2
      exit 1
    fi
    printf 'PASS: %-18s %s: json-file 20m x 5\n' "$service" "${id:0:12}"
  done <<< "$ids"
  if [[ "$service" == worker && "$count" -ne 2 ]]; then
    echo "FAIL: expected 2 worker containers, got $count" >&2
    exit 1
  fi
done
