"""One permanent run per accepted upload, independent of cache and filename."""
from pydantic import ValidationError
from app.archive_store import ArchiveStore
from app.catalogue_models import RunManifest, JobState, now
from app.document_repository import DocumentRepository, conflict, manifest_error
from app.errors import ProcessingError
from app.index_manager import IndexManager


class RunRepository:
    def __init__(self, archive: ArchiveStore, indexes: IndexManager, documents: DocumentRepository):
        self.archive, self.indexes, self.documents = archive, indexes, documents

    async def read(self, document_id, run_id):
        if await self.archive.run_deleted(run_id):
            raise ProcessingError('ARCHIVE_NOT_FOUND', 'Archived run was deleted.', 404)
        artifact = await self.archive.read_run_metadata(document_id, run_id)
        try:
            run = RunManifest.model_validate_json(artifact.data)
            if run.document_id != document_id or run.run_id != run_id:
                raise manifest_error()
            return run, artifact.etag
        except (ValidationError, ValueError) as exc:
            raise manifest_error() from exc

    async def get(self, document_id, run_id):
        return (await self.read(document_id, run_id))[0]

    async def _publish(self, run):
        if run.status == JobState.completed:
            doc = await self.documents.set_preferred(run.document_id, run.run_id, only_if_empty=True, publish=False)
        else:
            doc = await self.documents.get(run.document_id)
        await self.indexes.publish(run)
        await self.indexes.publish(doc)
        return run

    async def create(self, run: RunManifest):
        if run.revision != 1 or await self.archive.run_deleted(run.run_id):
            raise conflict()
        await self.documents.add_filename(run.document_id, run.filename, publish=False)
        try:
            await self.archive.write_run_metadata(run.document_id, run.run_id, run.model_dump(mode='json'))
        except ProcessingError as exc:
            if exc.code != 'ARCHIVE_CONFLICT':
                raise
            existing = await self.get(run.document_id, run.run_id)
            if existing != run:
                raise conflict() from exc
        return await self._publish(run)

    async def update(self, document_id, run_id, *, expected_revision: int, expected_fence=None, publish=True, **changes):
        # Callers supply their observed revision; the live pipeline additionally
        # verifies the durable fence and writes immutable artifacts before CAS.
        editable = {'status', 'cache', 'cache_source_run_id', 'page_count', 'started_at', 'completed_at',
                    'duration_ms', 'worker_attempts', 'input_tokens', 'output_tokens', 'cost_usd',
                    'trace_id', 'error_code', 'error_message', 'has_extraction', 'has_summary',
                    'worker_fence', 'retry_id', 'extraction_ref', 'summary_ref', 'error_diagnostic'}
        if not changes or not changes.keys() <= editable:
            raise ProcessingError('INVALID_RUN_UPDATE', 'Invalid run metadata update.')
        current, etag = await self.read(document_id, run_id)
        if current.revision != expected_revision or (expected_fence is not None and current.worker_fence != expected_fence):
            raise conflict()
        allowed = {JobState.queued: {JobState.queued, JobState.processing, JobState.failed, JobState.completed, JobState.partial},
            JobState.processing: {JobState.processing, JobState.summarizing, JobState.failed, JobState.partial, JobState.completed},
            JobState.summarizing: {JobState.summarizing, JobState.completed, JobState.partial},
            JobState.partial: {JobState.partial, JobState.summarizing, JobState.completed},
            JobState.completed: set(), JobState.failed: set()}
        if changes.get('status', current.status) not in allowed[current.status]:
            raise ProcessingError('INVALID_RUN_TRANSITION', 'Invalid run status transition.', 409)
        updated = RunManifest.model_validate({**current.model_dump(), **changes,
            'revision': current.revision + 1, 'updated_at': max(now(), current.updated_at)})
        try:
            await self.archive.write_run_metadata(document_id, run_id, updated.model_dump(mode='json'), expected_etag=etag)
        except ProcessingError as exc:
            if exc.code == 'ARCHIVE_CONFLICT':
                raise conflict() from exc
            raise
        return await self._publish(updated) if publish else updated
