"""Permanent document metadata; optimistic S3 CAS and revisioned projection."""
import asyncio
from pydantic import ValidationError
from app.archive_store import ArchiveStore, Original
from app.catalogue_models import DocumentManifest, JobState, now
from app.errors import ProcessingError
from app.index_manager import IndexManager


def manifest_error():
    return ProcessingError('ARCHIVE_MANIFEST_INVALID', 'Invalid archived catalogue manifest.', 500)


def conflict():
    return ProcessingError('CATALOGUE_CONFLICT', 'Catalogue manifest changed; reload and retry.', 409)


class DocumentRepository:
    def __init__(self, archive: ArchiveStore, indexes: IndexManager):
        self.archive, self.indexes = archive, indexes

    async def read(self, document_id):
        artifact = await self.archive.read_document_metadata(document_id)
        try:
            doc = DocumentManifest.model_validate_json(artifact.data)
            if doc.document_id != document_id or doc.s3_bucket != self.archive.bucket:
                raise manifest_error()
            if doc.revision <= await self.archive.document_revision_floor(document_id):
                raise ProcessingError('ARCHIVE_NOT_FOUND', 'Archived document was deleted.', 404)
            return doc, artifact.etag
        except (ValidationError, ValueError) as exc:
            raise manifest_error() from exc

    async def get(self, document_id):
        return (await self.read(document_id))[0]

    async def register(self, original: Original, filename: str):
        floor = await self.archive.document_revision_floor(original.document_id)
        doc = DocumentManifest(revision=floor + 1, document_id=original.document_id, sha256=original.document_id,
            filename=filename, filenames=(filename,), file_size_bytes=original.size_bytes,
            s3_bucket=self.archive.bucket, s3_object_key=original.key)
        try:
            await self.archive.write_document_metadata(doc.document_id, doc.model_dump(mode='json'))
        except ProcessingError as exc:
            if exc.code != 'ARCHIVE_CONFLICT':
                raise
            existing = await self.get(doc.document_id)
            if existing.file_size_bytes != doc.file_size_bytes:
                raise manifest_error()
            return await self.add_filename(doc.document_id, filename)
        await self.indexes.publish(doc)
        return doc

    async def _mutate(self, document_id, change, *, publish=True):
        for attempt in range(10):
            current, tag = await self.read(document_id)
            changes = change(current)
            if not changes:
                if publish:
                    await self.indexes.publish(current)
                return current
            updated = DocumentManifest.model_validate({**current.model_dump(), **changes,
                'revision': current.revision + 1, 'updated_at': max(now(), current.updated_at)})
            try:
                await self.archive.write_document_metadata(document_id, updated.model_dump(mode='json'), expected_etag=tag)
            except ProcessingError as exc:
                if exc.code != 'ARCHIVE_CONFLICT':
                    raise
                await asyncio.sleep(0.005 * (attempt + 1))
                continue
            if publish:
                await self.indexes.publish(updated)
            return updated
        raise conflict()

    async def add_filename(self, document_id, filename, *, publish=True):
        return await self._mutate(document_id, lambda d: {} if filename in d.filenames else {'filenames': (*d.filenames, filename)}, publish=publish)

    async def rename(self, document_id, display_name):
        # Keep historical names searchable; individual run filenames never change.
        def change(doc):
            if doc.filename == display_name:
                return {}
            return {'filename': display_name, 'filenames': doc.filenames if display_name in doc.filenames else (*doc.filenames, display_name)}
        return await self._mutate(document_id, change)

    async def set_preferred(self, document_id, run_id, *, only_if_empty=False, publish=True):
        # Never trust an index hit for ownership or successful status.
        from app.run_repository import RunRepository
        run = await RunRepository(self.archive, self.indexes, self).get(document_id, run_id)
        if run.status != JobState.completed:
            raise ProcessingError('INVALID_PREFERRED_RUN', 'Preferred run must be completed.', 409)
        def change(doc):
            if doc.preferred_run_id == run_id or (only_if_empty and doc.preferred_run_id is not None):
                return {}
            return {'preferred_run_id': run_id, 'page_count': run.page_count}
        return await self._mutate(document_id, change, publish=publish)
