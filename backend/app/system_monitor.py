"""Read-only operational snapshot using Redis, S3 and OpenSearch native APIs.

Worker state and document-cache counters are the only application instrumentation.
No PDF contents, names, IDs, API keys or remote model probes are exposed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import RedisError

from app.errors import ProcessingError
from app.archive_monitor import blank as empty_archive
from app.openrouter_key_monitor import empty_key_status
from app.docker_telemetry import docker_snapshot

log = logging.getLogger(__name__)
WORKER_KEY_PREFIX = "monitor:worker:"
CACHE_COUNTER_KEY = "monitor:cache:counts"


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def record_cache_outcome(redis: Any, outcome: int) -> None:
    """Best effort only: monitoring must never change an upload's outcome."""
    field = {0: "misses", 1: "shared", 2: "hits"}.get(int(outcome))
    if field is None:
        return
    try:
        await redis.hincrby(CACHE_COUNTER_KEY, field, 1)
    except Exception:
        log.debug("Cache telemetry unavailable", exc_info=True)


async def publish_worker_state(redis: Any, consumer: str, state: str,
                               parser: str | None, heartbeat_seconds: int,
                               container: str | None = None) -> None:
    """Ephemeral consumer presence; optional opaque container grouping ID."""
    if state not in {"idle", "extracting", "summarizing"}:
        return
    payload = json.dumps({"state": state, "parser": parser if state != "idle" else None,
                          "container": container})
    try:
        await redis.set(f"{WORKER_KEY_PREFIX}{consumer}", payload,
                        ex=max(15, heartbeat_seconds * 4))
    except Exception:
        log.debug("Worker telemetry unavailable", exc_info=True)


async def remove_worker_state(redis: Any, consumer: str) -> None:
    try:
        await redis.delete(f"{WORKER_KEY_PREFIX}{consumer}")
    except Exception:
        pass


async def _worker_states(redis: Any) -> list[dict]:
    """Use SCAN, never KEYS; cap work to keep snapshots inexpensive."""
    cursor: int | str = 0
    found: list[dict] = []
    for _ in range(16):
        cursor, keys = await redis.scan(cursor=cursor, match=f"{WORKER_KEY_PREFIX}*", count=128)
        for key in keys:
            if len(found) >= 128:
                break
            payload = await redis.get(key)
            if not payload:
                continue
            try:
                data = json.loads(payload)
                state = data["state"]
                if state not in {"idle", "extracting", "summarizing"}:
                    continue
                parser = data.get("parser")
                if parser not in {"pypdf", "gemini", "gpt5_mini"}:
                    parser = None
                # Expose only the random per-process suffix, not hostname or PID.
                suffix = str(key).rsplit("-", 1)[-1]
                container = data.get("container")
                if not isinstance(container, str) or not (len(container) == 8 and all(c in "0123456789abcdef" for c in container)):
                    container = None
                found.append({"id": suffix[:12], "state": state, "parser": parser,
                              "container": container})
            except (ValueError, TypeError, KeyError):
                continue
        if int(cursor) == 0 or len(found) >= 128:
            break
    return sorted(found, key=lambda worker: worker["id"])


async def _redis_snapshot(redis: Any, queue: Any) -> dict:
    await redis.ping()
    memory = await redis.info("memory")
    keys = await redis.dbsize()
    waiting = None
    pending = None
    try:
        groups = await redis.xinfo_groups(queue.stream)
        group = next((item for item in groups if item.get("name") == queue.group), None)
        if group is not None:
            waiting = _number(group["lag"]) if group.get("lag") is not None else None
            pending = _number(group.get("pending"))
    except RedisError:
        # The stream might not exist until the first worker starts.
        pass
    counts = await redis.hgetall(CACHE_COUNTER_KEY)
    workers = await _worker_states(redis)
    return {
        "status": "online",
        "memory_bytes": _number(memory.get("used_memory")),
        "memory_limit_bytes": _number(memory.get("maxmemory")),
        "key_count": _number(keys),
        "queue": {"waiting": waiting, "pending": pending},
        "workers": workers,
        "cache": {"hits": _number(counts.get("hits")), "misses": _number(counts.get("misses")),
                  "shared": _number(counts.get("shared"))},
    }


async def system_snapshot(redis: Any, object_store: Any, queue: Any,
                          openrouter_monitor: Any | None = None,
                          opensearch: Any | None = None, archive_monitor: Any | None = None) -> dict:
    """Return section-specific availability, not an all-or-nothing HTTP failure."""
    result = {
        "generated_at": _utc_now(),
        "api": {"status": "online"},
        "archive": empty_archive(),
        "redis": {"status": "unavailable", "memory_bytes": None, "memory_limit_bytes": None,
                  "key_count": None, "queue": {"waiting": None, "pending": None},
                  "workers": [], "cache": {"hits": None, "misses": None, "shared": None}},
        "rustfs": {"status": "unavailable", "objects": None, "bytes": None, "truncated": False},
        "openrouter": empty_key_status(),
        "opensearch": {"status": "unavailable", "cluster_health": None,
                       "heap_used_bytes": None, "heap_max_bytes": None,
                       "index_store_bytes": None, "node_count": None},
        "docker": {"status": "unavailable", "collected_at": None, "containers": []},
    }

    async def read_redis():
        try:
            result["redis"] = await asyncio.wait_for(_redis_snapshot(redis, queue), timeout=5)
        except Exception:
            log.warning("Monitoring: Redis metrics unavailable")

    async def read_storage():
        try:
            result["rustfs"] = {"status": "online", **await asyncio.wait_for(object_store.statistics(), timeout=8)}
        except Exception:
            log.warning("Monitoring: S3 metrics unavailable")

    async def read_openrouter():
        if openrouter_monitor is not None:
            try:
                result["openrouter"] = await asyncio.wait_for(openrouter_monitor.snapshot(), timeout=5)
            except Exception:
                log.warning("Monitoring: OpenRouter key metadata unavailable")
                result["openrouter"] = empty_key_status("unavailable")

    async def read_docker():
        try:
            result["docker"] = await asyncio.wait_for(docker_snapshot(redis), timeout=3)
        except Exception:
            log.debug("Docker telemetry unavailable", exc_info=True)

    async def read_opensearch():
        if opensearch is not None:
            try:
                result["opensearch"] = await asyncio.wait_for(opensearch.snapshot(), timeout=5)
            except Exception:
                log.warning("Monitoring: OpenSearch metrics unavailable")

    async def read_archive():
        if archive_monitor is not None:
            try:
                result["archive"] = await asyncio.wait_for(archive_monitor.snapshot(), timeout=8)
            except Exception:
                log.warning("Monitoring: archive metrics unavailable")

    await asyncio.gather(read_redis(), read_storage(), read_openrouter(), read_docker(), read_opensearch(), read_archive())
    return result
