"""Console-only archive operational tools; binary backup stream on stdout."""
import argparse
import asyncio
import json
import sys
import tempfile
from redis.asyncio import Redis
from app.archive_maintenance import ArchiveMaintenance
from app.archive_operations import execute, MAX_BACKUP
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.index_manager import IndexManager
from app.object_store import S3ObjectStore
from app.opensearch_client import OpenSearchClient
from app.persistent_pipeline import PersistentPipeline
from app.queue import DocumentQueue
from app.redis_store import DocumentRepository as RedisRepository
from app.run_repository import RunRepository
from app.errors import ProcessingError

async def main(action, stream=None):
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
        pipeline = PersistentPipeline(settings, docs, runs, repo)
        return await execute(ArchiveMaintenance(pipeline, queue, temporary), action, stream)
    finally:
        await redis.aclose()
        await search.aclose()
        temporary.client.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit','rebuild','backup','restore'))
    action = parser.parse_args().action
    try:
        if action == 'restore':
            # ZIP central directory requires seekable input. Stage on container disk,
            # not RAM; reject oversized inputs before any archive mutation.
            with tempfile.TemporaryFile() as source:
                total = 0
                while chunk := sys.stdin.buffer.read(1024*1024):
                    total += len(chunk)
                    if total > MAX_BACKUP + 128*1024**2:
                        raise ProcessingError('ARCHIVE_BACKUP_LIMIT', 'Backup input exceeds supported size.', 413)
                    source.write(chunk)
                source.seek(0)
                result = asyncio.run(main(action, source))
        else:
            result = asyncio.run(main(action, sys.stdout.buffer if action == 'backup' else None))
        print(json.dumps(result), file=sys.stderr if action == 'backup' else sys.stdout)
    except ProcessingError as exc:
        print(f'{exc.code}: {exc.message}', file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        # Avoid printing archive contents, credentials or untrusted ZIP names.
        print(f'Archive operation failed ({type(exc).__name__}). Check service health and restore marker.', file=sys.stderr)
        raise SystemExit(1)
