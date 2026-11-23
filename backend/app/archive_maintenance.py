"""Exclusive archive maintenance: replayable deletion and idle lost-queue recovery.

Run under the same named-volume flock as the API/worker processes. No direct
client writes or multi-host mounts are supported. Journal persists before any
irreversible operation, and blocks normal access until replay finishes.
"""
import asyncio
import logging
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from redis.exceptions import RedisError, ResponseError

from app.archive_gate import archive_gate, finish_before_unlock
from app.archive_store import _json
from app.cache import active_key, result_key
from app.catalogue_models import DocumentID, now
from app.errors import ProcessingError
from app.reconciliation import rebuild
from app.schemas import JobState

log = logging.getLogger(__name__)
TERMINAL = {JobState.completed, JobState.partial, JobState.failed}
JOURNAL = 'maintenance/deletion.json'


class DeleteRun(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    run_id: UUID
    fingerprint: DocumentID | None = None


class Deletion(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['run', 'document']
    document_id: DocumentID
    runs: tuple[DeleteRun, ...]
    revision_floor: int = Field(ge=0, lt=2**63-1, strict=True)

    @model_validator(mode='after')
    def valid(self):
        if len({r.run_id for r in self.runs}) != len(self.runs):
            raise ValueError('duplicate run')
        if self.kind == 'run' and (len(self.runs) != 1 or self.revision_floor != 0):
            raise ValueError('invalid run deletion')
        if self.kind == 'document' and not self.revision_floor:
            raise ValueError('invalid document deletion')
        return self


class ArchiveMaintenance:
    def __init__(self, pipeline, queue, temporary_store, *, path=None):
        self.p, self.queue, self.temporary_store, self.path = pipeline, queue, temporary_store, path
        self.archive = pipeline.archive
        self.redis = pipeline.repo.redis
        self.indexes = pipeline.documents.indexes

    async def idle(self):
        try:
            if await self.redis.xlen(self.queue.stream):
                raise ProcessingError('ARCHIVE_BUSY', 'Deletion and recovery require an empty processing queue.', 409)
            try:
                pending = await self.redis.xpending(self.queue.stream, self.queue.group)
            except ResponseError as exc:
                if 'NOGROUP' not in str(exc):
                    raise
            else:
                if pending['pending']:
                    raise ProcessingError('ARCHIVE_BUSY', 'Pending stream entries must finish before maintenance.', 409)
        except RedisError as exc:
            raise ProcessingError('REDIS_UNAVAILABLE', 'Redis is unavailable; archive maintenance is blocked.', 503) from exc

    async def document_runs(self, doc_id):
        records = []
        async for key in self.archive.iter_manifest_keys(doc_id):
            parts = key.split('/')
            if len(parts) == 4:
                records.append(await self.p.runs.get(doc_id, UUID(parts[2])))
        return records

    async def clear_run_cache(self, run):
        keys = [self.p.repo.key(run.run_id, suffix) for suffix in ('meta', 'pages', 'summary')]
        digest = run.fingerprint if isinstance(run, DeleteRun) else run.processing_fingerprint
        if digest and re.fullmatch(r'[0-9a-f]{64}', digest):
            keys.extend((active_key(digest), result_key(digest)))
        try:
            await self.redis.delete(*keys)
        except RedisError as exc:
            raise ProcessingError('REDIS_UNAVAILABLE', 'Redis cache invalidation failed.', 503) from exc
        await self.temporary_store.remove(run.run_id)

    async def clear_page_cache(self):
        # Page cache is shared/content-addressed and has no reverse document map.
        # Broad invalidation is intentional; never delete arbitrary Redis keys.
        try:
            while True:
                cursor, found = 0, 0
                while True:
                    cursor, keys = await self.redis.scan(cursor=cursor, match='pdfcache:v1:page:*', count=500)
                    if keys:
                        found += len(keys)
                        await self.redis.delete(*keys)
                    if not cursor:
                        break
                if not found:
                    break
        except RedisError as exc:
            raise ProcessingError('REDIS_UNAVAILABLE', 'Redis page cache invalidation failed.', 503) from exc

    async def marker(self, key, payload):
        current = await self.archive.optional(key)
        if current is not None and current.json() == payload:
            return
        await self.archive._write(key, _json(payload), 'application/json', current.etag if current else None)

    async def remove_prefix(self, prefix):
        # Do not mutate between list pages: some S3 implementations use offsets.
        keys = [key async for key in self.archive.iter_keys(prefix)]
        for key in keys:
            await self.archive.remove_key(key)
        if any([key async for key in self.archive.iter_keys(prefix)]):
            raise ProcessingError('ARCHIVE_DELETION_PENDING', 'Archive objects are still visible; deletion will be retried.', 503)

    async def journal(self):
        item = await self.archive.deletion_journal()
        if item is None:
            return None
        try:
            return Deletion.model_validate_json(item.data)
        except ValidationError as exc:
            raise ProcessingError('ARCHIVE_JOURNAL_INVALID', 'Deletion journal is invalid; maintenance halted.', 503) from exc

    async def request_delete(self, kind, identifier, *, expected_revision=None):
        async with archive_gate(exclusive=True, path=self.path):
            return await finish_before_unlock(self._delete(kind, identifier, expected_revision=expected_revision))

    async def _delete(self, kind, identifier, *, expected_revision=None):
        await self.archive.assert_no_restore()
        await self.idle()
        pending = await self.journal()
        if pending:
            same = pending.kind == kind and (pending.document_id == identifier if kind == 'document' else pending.runs[0].run_id == identifier)
            if same and kind == 'document' and expected_revision != pending.revision_floor - 1:
                raise ProcessingError('CATALOGUE_CONFLICT', 'Document revision changed; reload before deleting.', 412)
            if not same:
                raise ProcessingError('ARCHIVE_DELETION_PENDING', 'Another deletion is awaiting reconciliation.', 503)
            await self.finish(pending)
            return
        if kind == 'run':
            try:
                run = await self.p.get(identifier)
            except ProcessingError as exc:
                if exc.code == 'DOCUMENT_NOT_FOUND' and await self.archive.run_deleted(identifier):
                    return
                raise
            doc_id = run.document_id
            records = [run]
        else:
            doc_id = identifier
            try:
                existing = await self.p.documents.get(doc_id)
            except ProcessingError as exc:
                if exc.code == 'ARCHIVE_NOT_FOUND' and expected_revision is not None and await self.archive.document_revision_floor(doc_id) == expected_revision + 1:
                    return
                raise
            if expected_revision != existing.revision:
                raise ProcessingError('CATALOGUE_CONFLICT', 'Document revision changed; reload before deleting.', 412)
            records = await self.document_runs(doc_id)
        if any(r.status not in TERMINAL for r in records):
            raise ProcessingError('ACTIVE_RUN_DELETE_FORBIDDEN', 'An active processing run cannot be deleted.', 409)
        doc = await self.p.documents.get(doc_id)
        # Internal OpenSearch DELETE increments the old external version once.
        # Preserve a durable floor for a later upload of the same SHA-256.
        floor = doc.revision + 1 if kind == 'document' else 0
        planned = [DeleteRun(run_id=r.run_id, fingerprint=r.processing_fingerprint) for r in records]
        if kind == 'document':
            known = {r.run_id for r in planned}
            async for key in self.archive.iter_keys('run-ids/'):
                if re.fullmatch(r'run-ids/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.json', key):
                    uid = UUID(key[8:-5])
                    if uid not in known and await self.archive.locate_run(uid) == doc_id:
                        planned.append(DeleteRun(run_id=uid))
        plan = Deletion(kind=kind, document_id=doc_id, revision_floor=floor, runs=tuple(planned))
        await self.archive._write(JOURNAL, plan.model_dump_json().encode(), 'application/json')
        await self.finish(plan)

    async def finish(self, plan):
        await self.idle()
        doc_id = plan.document_id
        if plan.kind == 'document':
            floor = max(plan.revision_floor, await self.archive.document_revision_floor(doc_id))
            await self.marker(f'tombstones/documents/{doc_id}.json', {'revision_floor': floor})
        for run in plan.runs:
            await self.marker(f'tombstones/runs/{run.run_id}.json', {'document_id': doc_id})
            await self.clear_run_cache(run)
            await self.indexes.remove('runs', str(run.run_id))
            await self.remove_prefix(f'{doc_id}/runs/{run.run_id}/')
            await self.archive.remove_key(f'run-ids/{run.run_id}.json')
        await self.clear_page_cache()
        if plan.kind == 'document':
            await self.indexes.remove('documents', doc_id)
            await self.remove_prefix(f'{doc_id}/')
        else:
            removed = plan.runs[0].run_id
            remaining = await self.document_runs(doc_id)
            completed = [r for r in remaining if r.status == JobState.completed]
            fallback = max(completed, key=lambda r: (r.completed_at, str(r.run_id)), default=None)
            def prefer(doc):
                if doc.preferred_run_id != removed:
                    return {}
                return {'preferred_run_id': fallback.run_id if fallback else None, 'page_count': fallback.page_count if fallback else None}
            await self.p.documents._mutate(doc_id, prefer)
        await self.archive.remove_key(JOURNAL)

    async def recover(self, *, cleanup=False):
        async with archive_gate(exclusive=True, path=self.path):
            return await finish_before_unlock(self._recover(cleanup=cleanup))

    async def _recover(self, *, cleanup):
        await self.archive.assert_no_restore()
        await self.idle()
        await self.indexes.ensure()
        pending = await self.journal()
        if pending:
            await self.finish(pending)
        recovered = 0
        async for key in self.archive.iter_manifest_keys():
            parts = key.split('/')
            if len(parts) != 4:
                continue
            run = await self.p.runs.get(parts[0], UUID(parts[2]))
            if await self.archive.run_deleted(run.run_id):
                raise ProcessingError("ARCHIVE_INTEGRITY_ERROR", "A deleted run has an unexpected manifest; maintenance halted.", 503)
            await self.p.documents.get(run.document_id)
            await self.archive.write_run_locator(run.document_id, run.run_id)
            if run.status not in TERMINAL:
                finished = max(now(), run.updated_at)
                run = await self.p.runs.update(run.document_id, run.run_id, expected_revision=run.revision,
                    publish=False, status=JobState.partial if run.has_extraction else JobState.failed,
                    worker_fence=None, error_code='PROCESSING_INTERRUPTED', error_message='Processing queue was lost or admission interrupted. Retry the summary if extraction was saved.',
                    error_diagnostic=None, completed_at=finished, duration_ms=max(0, int((finished-run.created_at).total_seconds()*1000)))
                await self.clear_run_cache(run)
                recovered += 1
            elif run.status in {JobState.partial, JobState.failed}:
                # An accepted summary retry may be queued only in Redis. With
                # no stream entries it must not prevent a later manual retry.
                meta = await self.p.repo.get_meta(run.run_id)
                if meta and (meta.get('owner_token') or meta.get('status') == 'summarizing'):
                    await self.clear_run_cache(run)
            # Replay terminal publication including first-completed preference.
            await self.p.runs._publish(run)
        counts = await rebuild(self.p.documents, self.p.runs)
        counts.update(recovered=recovered, stale_index_records=await self.prune_index(),
                      orphan_objects=await self.cleanup_orphans() if cleanup else 0)
        await self.archive.remove_key("maintenance/reconcile-needed.json")
        return counts

    async def prune_index(self):
        # Only delete an exact stale projected record after checking canonical S3.
        # Collect first: do not mutate the OpenSearch result set mid-pagination.
        stale = []
        for kind in ('runs', 'documents'):
            async for identifier, doc_id, version in self.indexes.inventory(kind):
                key = f'{doc_id}/runs/{identifier}/metadata.json' if kind == 'runs' else f'{doc_id}/metadata.json'
                if await self.archive.optional(key) is None:
                    stale.append((kind, identifier, doc_id, version))
        for kind, identifier, doc_id, version in stale:
            if kind == 'documents':
                floor = max(version + 1, await self.archive.document_revision_floor(doc_id))
                await self.marker(f'tombstones/documents/{doc_id}.json', {'revision_floor': floor})
            else:
                await self.marker(f'tombstones/runs/{identifier}.json', {'document_id': doc_id})
            await self.indexes.remove(kind, identifier)
        return len(stale)

    async def cleanup_orphans(self):
        """Only known, unreachable keys; unknown or corrupt metadata fails closed.

        Original-only orphan and locator-only orphan are handled; unreferenced
        immutable result generations may be removed after processing is idle.
        """
        keys = [key async for key in self.archive.iter_keys()]
        keyset = set(keys)
        remove = []
        records = {}
        for key in keys:
            original = re.fullmatch(r'([0-9a-f]{64})/original\.pdf', key)
            locator = re.fullmatch(r'run-ids/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.json', key)
            artifact = re.fullmatch(r'([0-9a-f]{64})/runs/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/artifacts/([0-9a-f]{32})/(pages\.json|document\.md|summary\.md)', key)
            if original and f'{original[1]}/metadata.json' not in keyset:
                # Do not remove the original if any run exists (integrity issue).
                if not any(k.startswith(f'{original[1]}/runs/') for k in keyset):
                    remove.append(key)
            elif locator:
                uid = UUID(locator[1])
                doc = await self.archive.locate_run(uid)
                if f'{doc}/runs/{uid}/metadata.json' not in keyset:
                    remove.append(key)
            elif artifact:
                doc, uid, generation, name = artifact.groups()
                rid = UUID(uid)
                if f'{doc}/runs/{uid}/metadata.json' not in keyset:
                    remove.append(key)
                    continue
                if (doc, rid) not in records:
                    records[doc, rid] = await self.p.runs.get(doc, rid)
                run = records[doc, rid]
                ref = run.summary_ref if name == 'summary.md' else run.extraction_ref
                if generation != ref:
                    remove.append(key)
        for key in remove:
            await self.archive.remove_key(key)
        return len(remove)


async def recovery_loop(maintenance, *, interval=5.0, telemetry=None):
    """On API start and Redis server epoch change; replay pending deletions.

    A busy queue or unavailable dependency defers, never drops live work.
    Explicit reconciliation/GC remains available via the maintenance CLI.
    """
    last_epoch = None
    while True:
        try:
            epoch = (await maintenance.redis.info('server'))['run_id']
            pending = await maintenance.archive.deletion_journal()
            if epoch != last_epoch or pending is not None or await maintenance.archive.reconciliation_needed():
                counts = await maintenance.recover()
                last_epoch = epoch
                if telemetry is not None:
                    telemetry.update(state='completed', checked_at=now().isoformat(), recovered=counts['recovered'])
                log.info('Archive reconciliation complete: %s', counts)
        except (ProcessingError, RedisError, KeyError, ValidationError) as exc:
            if telemetry is not None:
                telemetry.update(state='deferred', checked_at=now().isoformat(), recovered=None)
            log.info('Archive reconciliation deferred: %s', getattr(exc, 'code', type(exc).__name__))
        await asyncio.sleep(interval)
