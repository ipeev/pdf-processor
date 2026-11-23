"""Bounded Docker resource telemetry sent by a host-side, non-root collector.

No Docker socket, host process commands, files, environment or container labels
enter the API. The payload contains only allowlisted service identifiers and
resource aggregates. All data expires; it is never used to make job decisions.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_LATEST_KEY = "monitor:docker:latest"
_HISTORY_KEY = "monitor:docker:history:"
_HISTORY_SECONDS = 300
_HISTORY_POINTS = 65


class ContainerSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    service: Literal["api", "frontend", "redis", "rustfs", "worker", "opensearch"]
    replica: int = Field(ge=1, le=32)
    id: str = Field(min_length=12, max_length=12, pattern=r"^[0-9a-f]{12}$")
    state: Literal["running", "exited", "created", "paused", "restarting", "dead", "unknown"]
    health: Literal["healthy", "unhealthy", "starting", "none", "unknown"]
    memory_used_bytes: int | None = Field(default=None, ge=0, le=1 << 60)
    memory_limit_bytes: int | None = Field(default=None, ge=1, le=1 << 60)
    cpu_percent: float | None = Field(default=None, ge=0, le=64000, allow_inf_nan=False)
    cpu_quota: float | None = Field(default=None, ge=0, le=2048, allow_inf_nan=False)
    cpuset_cpus: int | None = Field(default=None, ge=1, le=2048)
    processes: int | None = Field(default=None, ge=0, le=100000)
    threads: int | None = Field(default=None, ge=0, le=1000000)

    @model_validator(mode="after")
    def check_replica(self):
        if self.service != "worker" and self.replica != 1:
            raise ValueError("Only worker service may have multiple replicas")
        return self


class DockerBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    containers: list[ContainerSample] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_containers(self):
        identities = [(item.service, item.replica) for item in self.containers]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate service replica")
        if len({item.id for item in self.containers}) != len(self.containers):
            raise ValueError("Duplicate container ID")
        return self


async def receive_samples(redis, batch: DockerBatch) -> None:
    """Store an expiring latest snapshot and five-minute histories.

    Redis writes are intentionally independent of upload/worker transactions.
    """
    now = time.time()
    entries = [sample.model_dump(mode="json") for sample in batch.containers]
    for entry in entries:
        key = _HISTORY_KEY + entry["id"]
        history_item = json.dumps({"t": now, "cpu": entry["cpu_percent"], "memory": entry["memory_used_bytes"]}, separators=(",", ":"))
        await redis.lpush(key, history_item)
        await redis.ltrim(key, 0, _HISTORY_POINTS - 1)
        await redis.expire(key, 360)
    latest = {"timestamp": now, "containers": entries}
    # Publish only after all history entries are written.
    await redis.set(_LATEST_KEY, json.dumps(latest, separators=(",", ":")), ex=20)


async def docker_snapshot(redis) -> dict:
    """Report unavailable for an absent/stale collector. No synthetic zeroes."""
    result = {"status": "unavailable", "collected_at": None, "containers": []}
    raw = await redis.get(_LATEST_KEY)
    if not raw:
        return result
    payload = json.loads(raw)
    stamp = payload["timestamp"]
    if not isinstance(stamp, (int, float)) or abs(time.time() - stamp) > 20:
        return result
    containers = []
    for entry in payload["containers"][:16]:
        item = ContainerSample.model_validate(entry).model_dump(mode="json")
        raw_points = await redis.lrange(_HISTORY_KEY + item["id"], 0, _HISTORY_POINTS - 1)
        points = []
        for point in reversed(raw_points):
            data = json.loads(point)
            if stamp - _HISTORY_SECONDS <= data["t"] <= stamp + 5:
                points.append({"timestamp": datetime.fromtimestamp(data["t"], timezone.utc).isoformat(),
                               "cpu_percent": data["cpu"], "memory_used_bytes": data["memory"]})
        item["history"] = points
        containers.append(item)
    return {"status": "online", "collected_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "containers": containers}
