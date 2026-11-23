#!/usr/bin/env bash
# Verify actual shared named-volume flock semantics across API and worker.
set -euo pipefail
cd "$(dirname "$0")/.."
probe="/coordination/probe-$(date +%s)-$$"
holder=""
cleanup() {
  docker compose exec -T api sh -c 'touch "$1.release"' sh "$probe" >/dev/null 2>&1 || true
  if [[ -n "$holder" ]]; then wait "$holder" || true; fi
  docker compose exec -T api sh -c 'rm -f -- "$1" "$1.ready" "$1.release"' sh "$probe" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker compose exec -T api python -c '
import fcntl, pathlib, sys, time
p = pathlib.Path(sys.argv[1])
with p.open("a") as f:
    fcntl.flock(f, fcntl.LOCK_SH)
    pathlib.Path(str(p)+".ready").touch()
    for _ in range(300):
        if pathlib.Path(str(p)+".release").exists(): break
        time.sleep(.1)
    else: raise SystemExit("lock probe timed out")
' "$probe" &
holder=$!
ready=0
for _ in $(seq 1 30); do
  if docker compose exec -T --index 1 worker test -f "$probe.ready"; then ready=1; break; fi
  sleep .2
done
[[ "$ready" == 1 ]] || { echo 'FAIL: API and worker do not share the coordination volume.' >&2; exit 1; }
for index in 1 2; do
  docker compose exec -T --index "$index" worker python -c '
import fcntl, sys
with open(sys.argv[1], "a") as f:
    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    fcntl.flock(f, fcntl.LOCK_UN)
    try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: pass
    else: raise SystemExit("FAIL: exclusive lock acquired while API holds shared lock")
' "$probe"
done
docker compose exec -T --index 1 worker sh -c 'touch "$1.release"' sh "$probe"
wait "$holder"
holder=""
docker compose exec -T --index 2 worker python -c 'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)' "$probe"
echo 'PASS archive shared/exclusive coordination works across API and both workers.'
