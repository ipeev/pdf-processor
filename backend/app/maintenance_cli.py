"""Idle archive repair/reindex and optional safe orphan cleanup. No AI calls."""
import argparse
import asyncio
import json
from redis.asyncio import Redis
from app.archive_maintenance import ArchiveMaintenance
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.index_manager import IndexManager
from app.fulltext_search import PassageIndex
from app.object_store import S3ObjectStore
from app.opensearch_client import OpenSearchClient
from app.persistent_pipeline import PersistentPipeline
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository as RedisRepository
from app.run_repository import RunRepository
from app.errors import ProcessingError


async def main(cleanup=False):
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
    temporary = S3ObjectStore(settings)
    search = OpenSearchClient(settings.opensearch_url)
    try:
        archive = ArchiveStore(settings, client=temporary.client)
        docs = DocumentRepository(archive, IndexManager(search))
        runs = RunRepository(archive, docs.indexes, docs)
        repo = RedisRepository(redis, result_ttl=settings.result_ttl_seconds, cache_ttl=settings.cache_ttl_seconds)
        queue = DocumentQueue(redis, settings.stream_key, settings.consumer_group, settings.consumer_name)
        pipeline = PersistentPipeline(settings, docs, runs, repo, search_index=PassageIndex(search))
        print(json.dumps(await ArchiveMaintenance(pipeline, queue, temporary).recover(cleanup=cleanup)))
    finally:
        await redis.aclose()
        await search.aclose()
        temporary.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true', help='Remove known unreachable artifact generations and incomplete-admission objects')
    args = parser.parse_args()
    try:
        asyncio.run(main(args.cleanup))
    except ProcessingError as exc:
        raise SystemExit(f'{exc.code}: {exc.message}') from exc
