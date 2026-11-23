#!/usr/bin/env python3
"""Run inside the API container; no PDF data or external provider calls."""
import argparse
import asyncio
import re
import sys

from app.config import get_settings
from app.opensearch_client import OpenSearchClient
import httpx


async def run(mode: str, token: str | None) -> None:
    client = OpenSearchClient(get_settings().opensearch_url)
    try:
        if mode == 'check':
            info = await client.request('GET', '/')
            assert info['version']['distribution'] == 'opensearch', 'Unexpected search distribution'
            assert info['version']['number'] == '3.10.0', 'Unexpected OpenSearch version'
            metrics = await client.snapshot()
            assert metrics['status'] == 'online' and metrics['node_count'] == 1, 'OpenSearch not ready'
            assert metrics['heap_max_bytes'] > 0, 'JVM heap missing'
            print('PASS internal OpenSearch 3.10.0, single node, native metrics')
            return
        if token is None or re.fullmatch(r'[0-9a-f]{32}', token) is None:
            raise ValueError('Probe requires a 32-character hexadecimal token')
        index = f'pdf-stage1-probe-{token}'
        if mode == 'create':
            await client.request('PUT', f'/{index}', json={
                'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
                'mappings': {'properties': {'token': {'type': 'keyword'}}},
            })
            await client.request('PUT', f'/{index}/_create/probe', params={'refresh': 'wait_for'}, json={'token': token})
            print('PASS isolated index and test record created')
        elif mode == 'verify':
            doc = await client.request('GET', f'/{index}/_doc/probe')
            assert doc['found'] and doc['_source'] == {'token': token}, 'Probe record not recovered'
            hits = await client.request('POST', f'/{index}/_search', json={'query': {'term': {'token': token}}})
            assert [hit['_source'] for hit in hits['hits']['hits']] == [{'token': token}], 'Search probe failed'
            print('PASS exact record and search survived OpenSearch restart')
        elif mode == 'delete':
            try:
                await client.request('DELETE', f'/{index}')
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
            print('PASS isolated probe index removed')
    finally:
        await client.aclose()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['check', 'create', 'verify', 'delete'])
    p.add_argument('--token')
    args = p.parse_args()
    try:
        asyncio.run(run(args.mode, args.token))
    except Exception as exc:
        print(f'FAIL OpenSearch probe ({args.mode}): {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
