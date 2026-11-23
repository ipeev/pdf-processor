#!/usr/bin/env bash
# Check actual Docker port bindings, not just source Compose configuration.
# This is a single-user LOCALHOST-only release; it has no public auth boundary.
set -euo pipefail
cd "$(dirname "$0")/.."
for service in api frontend redis rustfs opensearch worker; do
  ids="$(docker compose ps -q --status running "$service")"
  [[ -n "$ids" ]] || { echo "FAIL: $service is not running." >&2; exit 1; }
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    bindings="$(docker inspect --format '{{range $port, $list := .NetworkSettings.Ports}}{{range $list}}{{println .HostIp}}{{end}}{{end}}' "$id")"
    case "$service" in
      api|frontend|rustfs)
        [[ -n "$bindings" ]] || { echo "FAIL: $service missing loopback mapping." >&2; exit 1; }
        while IFS= read -r ip; do
          [[ -z "$ip" || "$ip" == '127.0.0.1' ]] || { echo "FAIL: $service publishes to $ip (not loopback)." >&2; exit 1; }
        done <<< "$bindings"
        ;;
      *)
        [[ -z "$bindings" ]] || { echo "FAIL: internal $service has host port bindings." >&2; exit 1; }
        ;;
    esac
  done <<< "$ids"
done
printf 'PASS: frontend/API/RustFS exposed on loopback only; Redis/OpenSearch/workers have no host bindings.\n'
