"""Stage 9: isolated *actual* Redis container restart against real S3/OpenSearch.

Invoked by scripts/check_redis_restart_recovery.sh in three separate one-off API
containers: before / after / cleanup. All three services use disposable names.
No OpenRouter calls or production Redis/data/index writes are performed.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from uuid import UUID

import httpx
from redis.asyncio import Redis

from app.archive_maintenance import ArchiveMaintenance
from app.archive_store import ArchiveStore
from app.config import get_settings
from app.document_repository import DocumentRepository
from app.errors import ProcessingError
from app.index_manager import IndexManager
from app.opensearch_client import OpenSearchClient
from app.run_repository import RunRepository
from app.schemas import JobState
from tests.test_persistent_pipeline import Harness


assert len(sys.argv) == 3, 'Usage: python -m tests.rc_redis_restart before|after|cleanup 24-hex-token'
PHASE, TOKEN = sys.argv[1:]
assert PHASE in {'before', 'after', 'cleanup'}
assert len(TOKEN) == 24 and all(c in '0123456789abcdef' for c in TOKEN)


def data(number: int) -> bytes:
    # Appending a PDF comment keeps the fixture valid, unique and deterministic.
    return Path('/app/samples/basic_text.pdf').read_bytes() + f'\n% rc-redis-{TOKEN}-{number}\n'.encode()


def document_id(number: int) -> str:
    return hashlib.sha256(data(number)).hexdigest()


async def main():
    base = get_settings()
    settings = base.model_copy(update={
        's3_archive_bucket': f'pdf-rc1-{TOKEN}',
        'upload_dir': Path(f'/tmp/pdf-rc1-{TOKEN}'),
        'openrouter_api_key': base.openrouter_api_key.__class__(''),
    })
    settings.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
    store = ArchiveStore(settings)
    search = OpenSearchClient(settings.opensearch_url)
    # IndexManager deliberately accepts only the production 'pdf' prefix or
    # a pdf-test-<32-lowercase-hex> namespace. Derive a stable isolated
    # 32-character suffix from the 24-hex cleanup token for every phase.
    # A separate prefix is critical: never query or mutate production aliases.
    index_prefix = f"pdf-test-{hashlib.sha256(TOKEN.encode('ascii')).hexdigest()[:32]}"
    indexes = IndexManager(search, prefix=index_prefix)
    docs = DocumentRepository(store, indexes)
    runs = RunRepository(store, indexes, docs)
    h = Harness(settings, docs, runs, redis, namespace=f'rc-restart:{TOKEN}')
    try:
        if PHASE == 'cleanup':
            # *Only* the dedicated rc.1 temporary bucket and its two indexes.
            # No production document/index keys are ever selected.
            try:
                await store.ready()
            except ProcessingError as exc:
                if exc.code != 'ARCHIVE_UNAVAILABLE':
                    raise
            else:
                for key in [k async for k in store.iter_keys()]:
                    await store.remove_key(key)
                await asyncio.to_thread(store.client.delete_bucket, Bucket=store.bucket)
            for kind in ('documents', 'runs'):
                name, _ = indexes.names(kind)
                try:
                    await search.request('DELETE', f'/{name}')
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
            print('PASS: removed only isolated rc.1 archive bucket and indexes.')
            return

        await redis.ping()
        await store.prepare()
        await indexes.ensure()
        if PHASE == 'before':
            assert await redis.dbsize() == 0, 'Disposable Redis unexpectedly contains keys.'
            await h.queue.ensure_group()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url='http://rc-test') as client:
                done_resp, done_id = await h.upload(client, data(0), 'rc-completed.pdf')
                assert done_resp['cache'] == 'miss'
                await h.one()
                assert (await h.pipeline.get(done_id)).status == JobState.completed

                _, partial_id = await h.upload(client, data(1), 'rc-extraction.pdf')
                original_write = store.write_run_artifact

                async def fail_summary(*args, **kwargs):
                    if args[2] == 'summary.md':
                        raise ProcessingError('ARCHIVE_UNAVAILABLE', 'Simulated summary commit failure', 503)
                    return await original_write(*args, **kwargs)

                store.write_run_artifact = fail_summary
                try:
                    try:
                        await h.one()
                    except ProcessingError as exc:
                        assert exc.code == 'ARCHIVE_UNAVAILABLE'
                    else:
                        raise AssertionError('Expected interrupted summarization')
                finally:
                    store.write_run_artifact = original_write
                partial = await h.pipeline.get(partial_id)
                assert partial.has_extraction and not partial.has_summary
                assert partial.status == JobState.summarizing

                _, queued_id = await h.upload(client, data(2), 'rc-queued.pdf')
                assert (await h.pipeline.get(queued_id)).status == JobState.queued
                assert h.summary.calls == 2
            print('PASS: disposable Redis has completed, committed-extraction and queued jobs; mock AI only.')
        else:
            # The shell restarted the disposable Redis container, not production.
            assert await redis.dbsize() == 0, 'Redis did not lose volatile data during real container restart.'
            m = ArchiveMaintenance(h.pipeline, h.queue, h.store, path=settings.upload_dir / 'rc.lock')
            result = await m.recover()
            assert result['recovered'] == 2, result
            expected = ((0, JobState.completed, True, True),
                        (1, JobState.partial, True, False),
                        (2, JobState.failed, False, False))
            for n, state, extraction, summary in expected:
                keys = [key async for key in store.iter_manifest_keys(document_id(n))]
                records = [k for k in keys if k.endswith('/metadata.json') and '/runs/' in k]
                assert len(records) == 1, (n, records)
                run_id = UUID(records[0].split('/')[2])
                run = await h.pipeline.get(run_id)
                assert run.status == state and run.has_extraction == extraction and run.has_summary == summary, run
                if n == 0:
                    assert await h.pipeline.read_summary(run) == 'Summary: α + $x^2$'
                if n == 1:
                    assert run.error_code == 'PROCESSING_INTERRUPTED'
            assert h.summary.calls == 0, 'Recovery unexpectedly ran AI'
            repeated = await m.recover()
            assert repeated['recovered'] == 0, repeated
            print('PASS: actual isolated Redis restart recovered 2 interrupted jobs; durable completion unchanged; idempotent; no AI.')
    finally:
        await redis.aclose()
        await search.aclose()
        store.client.close()


if __name__ == '__main__':
    asyncio.run(main())
