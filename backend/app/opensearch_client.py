"""Internal OpenSearch REST client. Stage 1 only reads infrastructure metrics.

Reuse the existing HTTPX dependency; no additional SDK or provider calls.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx


class OpenSearchClient:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=url.rstrip('/') + '/', timeout=httpx.Timeout(3.0),
            trust_env=False, transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict:
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('OpenSearch path must be local')
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Invalid OpenSearch response')
        return data

    @staticmethod
    def _count(value: Any) -> int:
        if type(value) is not int or value < 0:
            raise ValueError('Invalid OpenSearch count')
        return value

    async def snapshot(self) -> dict:
        health, jvm, stats = await asyncio.gather(
            self.request('GET', '/_cluster/health', params={'filter_path': 'status,number_of_nodes'}),
            self.request('GET', '/_nodes/_local/stats/jvm', params={'filter_path': 'nodes.*.jvm.mem.heap_used_in_bytes,nodes.*.jvm.mem.heap_max_in_bytes'}),
            self.request('GET', '/_stats/store', params={'filter_path': '_all.total.store.size_in_bytes'}),
        )
        cluster_health = health['status']
        if cluster_health not in ('green', 'yellow', 'red'):
            raise ValueError('Invalid OpenSearch cluster health')
        nodes = list(jvm['nodes'].values())
        if len(nodes) != 1:
            raise ValueError('Expected local node metrics')
        mem = nodes[0]['jvm']['mem']
        return {
            'status': 'unavailable' if cluster_health == 'red' else 'online',
            'cluster_health': cluster_health,
            'node_count': self._count(health['number_of_nodes']),
            'heap_used_bytes': self._count(mem['heap_used_in_bytes']),
            'heap_max_bytes': self._count(mem['heap_max_in_bytes']),
            'index_store_bytes': self._count(stats['_all']['total']['store']['size_in_bytes']),
        }
