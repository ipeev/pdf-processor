"""Offline OpenSearch passage-index administration. No OpenRouter calls.

Usage (inside the API container):
    python -m app.search_cli init
    python -m app.search_cli rebuild

Rebuild takes an exclusive archive lock, requires an idle processing queue,
and recreates *only* the derived pdf-passages-v1 index from RustFS.
"""
import argparse
import asyncio
import json

from redis.asyncio import Redis
from app.archive_gate import archive_gate, finish_before_unlock
from app.archive_maintenance import ArchiveMaintenance
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.fulltext_search import PassageIndex
from app.index_manager import IndexManager
from app.object_store import S3ObjectStore
from app.opensearch_client import OpenSearchClient
from app.persistent_pipeline import PersistentPipeline
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository as RedisRepository
from app.run_repository import RunRepository


async def main(action):
    settings = get_settings()
    search = OpenSearchClient(settings.opensearch_url)
    redis = None
    temp = None
    try:
        index = PassageIndex(search)
        if action == 'init':
            await index.ensure()
            return {'search_index': 'ready', 'name': 'pdf-passages-v1'}
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        temp = S3ObjectStore(settings)
        archive = ArchiveStore(settings, client=temp.client)
        docs = DocumentRepository(archive, IndexManager(search))
        runs = RunRepository(archive, docs.indexes, docs)
        repo = RedisRepository(redis, result_ttl=settings.result_ttl_seconds, cache_ttl=settings.cache_ttl_seconds)
        queue = DocumentQueue(redis, settings.stream_key, settings.consumer_group, settings.consumer_name)
        pipeline = PersistentPipeline(settings, docs, runs, repo, search_index=index)
        maintenance = ArchiveMaintenance(pipeline, queue, temp)
        async with archive_gate(exclusive=True):
            async def operation():
                await maintenance.idle()
                await archive.assert_no_restore()
                await archive.assert_no_deletion()
                await archive.ready()
                return await index.rebuild_all(pipeline)
            return await finish_before_unlock(operation())
    finally:
        if redis:
            await redis.aclose()
        await search.aclose()
        if temp:
            temp.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'rebuild'))
    try:
        print(json.dumps(asyncio.run(main(parser.parse_args().action)), sort_keys=True))
    except ProcessingError as exc:
        raise SystemExit(f'{exc.code}: {exc.message}') from exc
