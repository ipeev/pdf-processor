"""No-network contracts. Real restart probe is mandatory in verify.sh."""
from pathlib import Path
import httpx
import pytest

from app.opensearch_client import OpenSearchClient

ROOT = Path(__file__).resolve().parents[2]


def reply(request):
    samples = {
        '/_cluster/health': {'status': 'green', 'number_of_nodes': 1},
        '/_nodes/_local/stats/jvm': {'nodes': {'hidden-id': {'jvm': {'mem': {'heap_used_in_bytes': 20, 'heap_max_in_bytes': 100}}}}},
        '/_stats/store': {'_all': {'total': {'store': {'size_in_bytes': 42}}}},
    }
    assert request.method == 'GET'
    assert request.url.host == 'opensearch'
    return httpx.Response(200, json=samples[request.url.path])


@pytest.mark.asyncio
async def test_snapshot_exposes_only_aggregate_metrics():
    client = OpenSearchClient('http://opensearch:9200', transport=httpx.MockTransport(reply))
    try:
        assert await client.snapshot() == {'status': 'online', 'cluster_health': 'green',
            'node_count': 1, 'heap_used_bytes': 20, 'heap_max_bytes': 100, 'index_store_bytes': 42}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_red_cluster_preserves_observed_health():
    def red(request):
        if request.url.path == '/_cluster/health':
            return httpx.Response(200, json={'status': 'red', 'number_of_nodes': 1})
        return reply(request)
    client = OpenSearchClient('http://opensearch:9200', transport=httpx.MockTransport(red))
    try:
        result = await client.snapshot()
        assert result['status'] == 'unavailable' and result['cluster_health'] == 'red'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bad_response_and_external_path_rejected():
    client = OpenSearchClient('http://opensearch:9200', transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.snapshot()
        with pytest.raises(ValueError):
            await client.request('GET', 'https://example.org')
    finally:
        await client.aclose()


def test_private_compose_and_verification_contract():
    source = (ROOT / 'docker-compose.yml').read_text()
    block = source.split('\n  opensearch:\n')[1].split('\n  api:\n')[0]
    assert 'image: opensearchproject/opensearch:3.10.0' in block
    assert 'opensearch_data:/usr/share/opensearch/data' in block
    assert 'ports:' not in block
    assert 'search_internal' in block
    assert 'internal: true' in source
    assert 'OPENSEARCH_JAVA_OPTS: "-Xms1g -Xmx1g"' in block
    assert 'opensearch_data:\n' in source
    assert './scripts/check_opensearch.sh --restart' in (ROOT / 'scripts/verify.sh').read_text()
    assert './scripts/check_opensearch.sh --check' in (ROOT / 'scripts/setup.sh').read_text()


@pytest.mark.asyncio
async def test_monitor_outage_is_section_local():
    from app.system_monitor import system_snapshot
    from tests.fake_redis import FakeRedis
    from app.queue import DocumentQueue
    class Storage:
        async def statistics(self):
            return {'objects': 0, 'bytes': 0, 'truncated': False}
    class Search:
        async def snapshot(self):
            raise RuntimeError('simulated outage')
    redis = FakeRedis()
    queue = DocumentQueue(redis, 'probe-stream', 'probe-group', 'probe-consumer')
    data = await system_snapshot(redis, Storage(), queue, opensearch=Search())
    assert data['opensearch']['status'] == 'unavailable'
    assert data['opensearch']['heap_used_bytes'] is None
    assert data['rustfs']['status'] == 'online'
    assert data['api']['status'] == 'online'
