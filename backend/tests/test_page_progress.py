"""Per-page status is live, fenced and optional; no paid model calls."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.catalogue_models import RunManifest
from app.errors import ProcessingError
from app.persistent_pipeline import PersistentPipeline
from app.redis_store import DocumentRepository
from tests.fake_redis import FakeRedis


@pytest.mark.asyncio
async def test_progress_is_fenced_and_cleared_for_new_attempts():
    fake = FakeRedis()
    repo = DocumentRepository(fake)
    run = uuid4()
    key = repo.key(run, 'meta')
    await fake.hset(key, mapping={'owner_token': 'worker-a', 'status': 'processing'})
    await repo.set_page_progress(run, 5, 21, owner_token='worker-a', attempt=2, max_attempts=3)
    progress = repo.parse_page_progress(await repo.get_meta(run))
    assert progress.model_dump() == {'page': 5, 'pages_total': 21, 'attempt': 2, 'max_attempts': 3}
    with pytest.raises(ProcessingError, match='JOB_LEASE_LOST'):
        await repo.set_page_progress(run, 6, 21, owner_token='worker-b')
    assert repo.parse_page_progress(await repo.get_meta(run)).page == 5

    # Summarization hides the counter, and a new claim resets old values.
    await repo.mark_summarizing(run, owner_token='worker-a')
    assert repo.parse_page_progress(await repo.get_meta(run)) is None
    await repo.mark_processing(run, owner_token='worker-a')
    assert repo.parse_page_progress(await repo.get_meta(run)) is None


@pytest.mark.asyncio
async def test_archive_status_exposes_progress_only_while_processing():
    fake = FakeRedis()
    repo = DocumentRepository(fake)
    run_id = uuid4()
    document_id = 'a' * 64
    manifest = RunManifest(run_id=run_id, document_id=document_id, filename='input.pdf',
        parser='vision_figures', pipeline_version='test', application_version='0.12.8',
        s3_prefix=f'{document_id}/runs/{run_id}/', status='processing')
    pipeline = PersistentPipeline(SimpleNamespace(), None, SimpleNamespace(archive=object()), repo)
    async def get(_): return manifest
    pipeline.get = get
    await fake.hset(repo.key(run_id, 'meta'), mapping={'owner_token': 'worker', 'status': 'processing'})
    assert (await pipeline.status(run_id)).page_progress is None
    await repo.set_page_progress(run_id, 12, 34, owner_token='worker')
    assert (await pipeline.status(run_id)).page_progress.pages_total == 34

    # A missing Redis key is not a reason to fail the archive status endpoint.
    await fake.delete(repo.key(run_id, 'meta'))
    assert (await pipeline.status(run_id)).status == 'processing'
    assert (await pipeline.status(run_id)).page_progress is None


def test_bad_progress_is_never_accepted_as_valid_status():
    repo = DocumentRepository(FakeRedis())
    for record in (
        {'status': 'processing', 'progress_page': '0', 'progress_total': '21',
         'progress_attempt': '1', 'progress_max_attempts': '3'},
        {'status': 'processing', 'progress_page': '22', 'progress_total': '21',
         'progress_attempt': '1', 'progress_max_attempts': '3'},
        {'status': 'processing', 'progress_page': '5', 'progress_total': '21',
         'progress_attempt': '4', 'progress_max_attempts': '3'},
        {'status': 'completed', 'progress_page': '5', 'progress_total': '21',
         'progress_attempt': '1', 'progress_max_attempts': '3'},
    ):
        assert repo.parse_page_progress(record) is None
