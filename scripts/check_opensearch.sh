#!/usr/bin/env bash
# Functional test only. --restart restarts just OpenSearch and uses an isolated
# synthetic index; does not touch PDFs, Redis, existing indexes or volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:---check}"
if [[ "$#" -gt 1 || ( "$mode" != --check && "$mode" != --restart ) ]]; then
  echo 'Usage: ./scripts/check_opensearch.sh [--check|--restart]' >&2; exit 2
fi
probe() { docker compose exec -T api python /app/scripts/check_opensearch.py "$@"; }
wait_search() {
  for ((attempt=1;attempt<=60;attempt++)); do
    if probe check >/dev/null 2>&1; then probe check; return; fi
    sleep 2
  done
  echo 'OpenSearch readiness failed. Recent logs:' >&2
  docker compose logs --tail=60 opensearch >&2
  return 1
}
wait_search
runtime_uid="$(docker compose exec -T opensearch id -u | tr -d '\r')"
if [[ ! "$runtime_uid" =~ ^[1-9][0-9]*$ ]]; then
  echo 'FAIL OpenSearch must not run as root' >&2; exit 1
fi
# No host port mapping; network isolation is tested separately in static tests.
container_id="$(docker compose ps -q opensearch)"
[[ -n "$container_id" ]] || { echo 'FAIL OpenSearch container not found' >&2; exit 1; }
published="$(docker inspect --format '{{if or .HostConfig.PublishAllPorts (eq .HostConfig.NetworkMode "host")}}unsafe-network {{end}}{{range $port, $bindings := .HostConfig.PortBindings}}{{if $bindings}}{{$port}} {{end}}{{end}}{{range $port, $bindings := .NetworkSettings.Ports}}{{if $bindings}}{{$port}} {{end}}{{end}}' "$container_id")"
if [[ -n "$published" ]]; then
  echo "FAIL OpenSearch must not publish host ports: $published" >&2; exit 1
fi
printf 'PASS OpenSearch has no published host ports\n'
if [[ "$mode" == --check ]]; then exit 0; fi
token="$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]')"
cleanup() { probe delete --token "$token" || echo 'WARNING: test index cleanup failed' >&2; }
trap cleanup EXIT
probe create --token "$token"
docker compose restart opensearch
wait_search
probe verify --token "$token"
probe delete --token "$token"
trap - EXIT
