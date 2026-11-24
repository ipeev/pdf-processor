"""Stage 4: archive-first processing; Redis is coordination and a result cache.

A worker claims a durable fence via S3 CAS. Artifacts use fresh immutable
keys, then a single conditional manifest publication makes them visible.
A superseded worker may leave unreachable objects, but cannot replace a
committed result. Index publication is retried from the manifest before ACK.
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from app.cache import PROMPT_VERSION, fingerprint
from app.figure_assets import metadata_from_result, validate_metadata, verify_png, invalid as invalid_figures
from app.parsers.vision_figures import VisionResult
from app.catalogue_models import RunManifest, now
from app.errors import ProcessingError
from app.observability import current_ids, inject, span
from app.provider_diagnostics import sanitize_diagnostic
from app.redis_store import DocumentRepository as RedisRepository
from redis.exceptions import RedisError
from app.schemas import DocumentStatus, JobState, PageList, SafeError

log = logging.getLogger(__name__)
TERMINAL = {JobState.completed, JobState.partial, JobState.failed}


def lease_lost():
    return ProcessingError('JOB_LEASE_LOST', 'Job ownership changed before archive commit.', 409)


def encode_pages(parsed):
    parsed.validate()
    return [{'page': p.page, 'markdown': p.markdown} for p in parsed.pages]


class PersistentPipeline:
    def __init__(self, settings, documents, runs, repo, *, search_index=None):
        self.settings, self.documents, self.runs, self.repo = settings, documents, runs, repo
        self.archive = runs.archive
        self.search_index = search_index

    async def register(self, item, digest):
        original = await self.archive.put_original(item.temp_path, expected_sha256=item.sha256)
        await self.documents.register(original, item.filename)
        trace_id, upload_id = current_ids()
        run = RunManifest(run_id=item.id, document_id=original.document_id,
            filename=item.filename, parser=item.parser, upload_id=UUID(upload_id) if upload_id else None,
            trace_id=trace_id or None, pipeline_version=PROMPT_VERSION, application_version='0.12.6',
            extraction_model={'gemini': self.settings.native_pdf_model, 'gpt5_mini': self.settings.image_ocr_model, 'vision_figures': self.settings.vision_figures_model}.get(item.parser.value),
            summary_model=self.settings.summary_model, processing_fingerprint=digest,
            s3_prefix=f'{original.document_id}/runs/{item.id}/')
        await self.archive.write_run_locator(original.document_id, item.id)
        return await self.runs.create(run)

    async def get(self, run_id):
        try:
            document_id = await self.archive.locate_run(run_id)
            return await self.runs.get(document_id, run_id)
        except ProcessingError as exc:
            if exc.code == 'ARCHIVE_NOT_FOUND':
                raise ProcessingError('DOCUMENT_NOT_FOUND', 'Document does not exist.', 404) from exc
            raise

    async def record_registration(self, run, meta):
        """Record cache decision, even if Redis's 24h result cache later expires.

        Worker claim may win the CAS; it applies the same decision. Never
        overwrite a claimed run. Redis registration and archive are not an XA transaction.
        """
        current = await self.get(run.run_id)
        changes = self.cache_fields(meta)
        if current.worker_fence or current.status != JobState.queued or all(getattr(current, k) == v for k, v in changes.items()):
            return current
        try:
            updated = await self.runs.update(current.document_id, current.run_id, expected_revision=current.revision, publish=False, **changes)
            await self.publish_intermediate(updated)
            return updated
        except ProcessingError as exc:
            if exc.code != 'CATALOGUE_CONFLICT':
                raise
            return await self.get(run.run_id)

    @staticmethod
    def cache_fields(meta):
        return {'cache': meta.get('cache', 'miss'), 'cache_source_run_id': UUID(meta['cache_source_id']) if meta.get('cache_source_id') else None}

    async def require_owner(self, run_id, token):
        meta = await self.repo.get_meta(run_id)
        if not meta or meta.get('owner_token') != token:
            raise lease_lost()
        return meta

    async def claim(self, run_id, token, task):
        for _ in range(10):
            run = await self.get(run_id)
            meta = await self.require_owner(run_id, token)
            retry_id = meta.get('archive_retry_id') if task == 'summary' else None
            if task == 'summary' and not retry_id:
                raise ProcessingError('INVALID_JOB', 'Summary retry is missing its identifier.')
            if run.status in TERMINAL and not (task == 'summary' and run.status == JobState.partial and run.retry_id != retry_id):
                return run
            status = JobState.summarizing if run.has_extraction else JobState.processing
            attempts = (0 if run.status == JobState.partial else run.worker_attempts) + 1
            changes = dict(worker_fence=token, status=status, started_at=run.started_at or now(),
                completed_at=None, duration_ms=None, worker_attempts=attempts,
                error_code=None, error_message=None, error_diagnostic=None)
            if task == 'summary':
                changes['retry_id'] = retry_id
            else:
                changes.update(self.cache_fields(meta))
            try:
                updated = await self.runs.update(run.document_id, run_id, expected_revision=run.revision, publish=False, **changes)
                await self.publish_intermediate(updated)
                return updated
            except ProcessingError as exc:
                if exc.code != 'CATALOGUE_CONFLICT':
                    raise
                await asyncio.sleep(.01)
        raise lease_lost()

    async def commit(self, run, token, **changes):
        await self.require_owner(run.run_id, token)
        if run.worker_fence != token:
            raise lease_lost()
        try:
            updated = await self.runs.update(run.document_id, run.run_id, expected_revision=run.revision,
                expected_fence=token, publish=False, **changes)
            if updated.status not in TERMINAL:
                await self.publish_intermediate(updated)
            return updated
        except ProcessingError as exc:
            if exc.code == 'CATALOGUE_CONFLICT':
                raise lease_lost() from exc
            raise

    async def publish_intermediate(self, run):
        try:
            await self.runs._publish(run)
        except ProcessingError as exc:
            if exc.code != 'CATALOGUE_UNAVAILABLE':
                raise
            log.warning('Catalogue projection deferred for run %s', run.run_id)

    async def save_extraction(self, run, token, parsed, *, source_run=None):
        """Publish a complete text/image generation with one fenced manifest CAS.

        Source-run clones always receive independent image keys so later deletion
        of a source run cannot invalidate cached or shared results.
        """
        await self.require_owner(run.run_id, token)
        is_vision = run.parser.value == 'vision_figures'
        if is_vision:
            if isinstance(parsed, VisionResult):
                pages_doc = parsed.as_parsed_document()
                figure_metadata = metadata_from_result(parsed)
                source_images = {asset.path.removeprefix('images/'): asset.data for asset in parsed.assets}
            elif source_run is not None:
                pages_doc = parsed
                figure_metadata = await self.read_figures(source_run, pages_doc)
                source_images = {entry['filename']: await self.read_figure(source_run, entry['filename'])
                                 for entry in figure_metadata['figures']}
            else:
                raise ProcessingError('VISION_CACHE_SOURCE_MISSING',
                                      'Figure cache cannot be cloned without a durable source run.')
        else:
            if isinstance(parsed, VisionResult):
                raise ProcessingError('INVALID_EXTRACTION', 'Non-vision parser cannot publish pictures.')
            pages_doc = parsed
            figure_metadata = None
            source_images = {}
        pages = encode_pages(pages_doc)
        generation = uuid4().hex
        raw = json.dumps(pages, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        markdown = '\n\n---\n\n'.join(f"# Page {p['page']}\n\n{p['markdown'].strip() or '_(Blank page)_'}" for p in pages) + '\n'
        if is_vision:
            figure_metadata = validate_metadata(figure_metadata, pages_doc.pages)
            for entry in figure_metadata['figures']:
                data = verify_png(source_images[entry['filename']], entry)
                await self.archive.write_figure(run.document_id, run.run_id, generation, entry['filename'], data)
            await self.archive.write_run_artifact(run.document_id, run.run_id, 'figures.json',
                json.dumps(figure_metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode(), generation=generation)
        await self.archive.write_run_artifact(run.document_id, run.run_id, 'pages.json', raw, generation=generation)
        await self.archive.write_run_artifact(run.document_id, run.run_id, 'document.md', markdown.encode(), generation=generation)
        # A failed commit leaves unreachable immutable objects; it never exposes
        # partially written Markdown/image sets. Reconciler handles orphans.
        return await self.commit(run, token, status=JobState.summarizing, has_extraction=True,
                                 page_count=len(pages), extraction_ref=generation)

    async def read_figures(self, run, parsed=None):
        if run.parser.value != 'vision_figures':
            return {'version': 1, 'figures': [], 'warnings': []}
        if not run.has_extraction or not run.extraction_ref:
            raise ProcessingError('RESULT_NOT_READY', 'Figures are not yet committed.', 409)
        try:
            artifact = await self.archive.read_run_artifact(run.document_id, run.run_id, 'figures.json', generation=run.extraction_ref)
            data = json.loads(artifact.data)
            pages_doc = parsed if parsed is not None else await self.read_extraction(run)
            return validate_metadata(data, pages_doc.pages)
        except (ValueError, UnicodeError, TypeError, KeyError) as exc:
            raise invalid_figures() from exc

    async def read_figure(self, run, name):
        import re
        if run.parser.value != 'vision_figures' or not isinstance(name, str) or not re.fullmatch(r'p[0-9]{4}-figure-[A-Za-z0-9_-]{1,70}\.png', name):
            raise ProcessingError('FIGURE_NOT_FOUND', 'Figure is not present.', 404)
        figures = await self.read_figures(run)
        entry = next((f for f in figures['figures'] if f['filename'] == name), None)
        if entry is None:
            raise ProcessingError('FIGURE_NOT_FOUND', 'Figure is not present.', 404)
        try:
            raw = await self.archive.read_figure(run.document_id, run.run_id, run.extraction_ref,
                                                 name, max_bytes=self.settings.max_vision_asset_bytes)
        except ProcessingError as exc:
            if exc.code == 'ARCHIVE_NOT_FOUND':
                raise invalid_figures('Committed figure asset is missing.') from exc
            raise
        return verify_png(raw, entry)

    async def read_extraction(self, run):
        if not run.has_extraction or not run.extraction_ref:
            raise ProcessingError('RESULT_NOT_READY', 'Extracted pages are not available yet.', 409)
        artifact = await self.archive.read_run_artifact(run.document_id, run.run_id, 'pages.json', generation=run.extraction_ref)
        try:
            raw = artifact.data.decode('utf-8')
        except UnicodeError as exc:
            raise ProcessingError('RESULT_CORRUPT', 'Archived pages are inconsistent.', 503) from exc
        return RedisRepository._decode_pages(raw, {'page_count': run.page_count})

    async def read_summary(self, run):
        if not run.has_summary or not run.summary_ref:
            raise ProcessingError('RESULT_NOT_READY', 'Summary is not available yet.', 409)
        artifact = await self.archive.read_run_artifact(run.document_id, run.run_id, 'summary.md', generation=run.summary_ref)
        try:
            text = artifact.data.decode('utf-8')
        except UnicodeError as exc:
            raise ProcessingError('RESULT_CORRUPT', 'Archived summary is inconsistent.', 503) from exc
        if not text.strip():
            raise ProcessingError('RESULT_CORRUPT', 'Archived summary is inconsistent.', 503)
        return text

    async def complete(self, run, token, summary):
        if not isinstance(summary, str) or not summary.strip():
            raise ProcessingError('EMPTY_SUMMARY', 'Summarization returned empty content.')
        await self.require_owner(run.run_id, token)
        generation = uuid4().hex
        await self.archive.write_run_artifact(run.document_id, run.run_id, 'summary.md', summary.strip().encode(), generation=generation)
        finished = now()
        return await self.commit(run, token, status=JobState.completed, has_summary=True, summary_ref=generation,
            completed_at=finished, duration_ms=max(0, int((finished-run.created_at).total_seconds()*1000)))

    async def fail(self, run, token, error):
        finished = now()
        return await self.commit(run, token, status=JobState.partial if run.has_extraction else JobState.failed,
            error_code=error.code, error_message=error.message,
            error_diagnostic=sanitize_diagnostic(error.diagnostic) if error.diagnostic else None,
            completed_at=finished, duration_ms=max(0, int((finished-run.created_at).total_seconds()*1000)))

    async def project_terminal(self, run, token):
        # Idempotent on redelivery, including a crash after the archive commit
        # but before the index/Redis acknowledgement. Never invoke AI here.
        await self.require_owner(run.run_id, token)
        await self.runs._publish(run)
        # Search is a rebuildable projection. Never replay paid extraction if
        # OpenSearch is unavailable; operators can run `search_cli rebuild`.
        if self.search_index and run.has_extraction:
            try:
                await self.search_index.rebuild_document(self, run.document_id)
            except Exception as exc:
                # Derived-index failure must not prevent Redis ACK or trigger any
                # additional paid work. Operators can rebuild from RustFS.
                log.warning('Search projection deferred for document=%s error=%s', run.document_id, type(exc).__name__)
        if run.status == JobState.completed:
            await self.repo.store_completed(run.run_id, await self.read_extraction(run), await self.read_summary(run), owner_token=token)
        else:
            error = ProcessingError(run.error_code or 'PROCESSING_FAILED', run.error_message or 'Processing failed.', diagnostic=run.error_diagnostic)
            if run.status == JobState.partial:
                await self.repo.store_extracted(run.run_id, await self.read_extraction(run), owner_token=token)
                await self.repo.store_partial(run.run_id, error, owner_token=token)
            else:
                await self.repo.store_failed(run.run_id, error, owner_token=token)

    async def process(self, run_id, token, task, processor):
        meta = await self.require_owner(run_id, token)
        source = None
        initial = await self.get(run_id)
        if initial.document_id != meta.get('archive_document_id') or initial.parser.value != meta.get('parser'):
            raise ProcessingError('INVALID_JOB', 'Job metadata differs from permanent identity.')
        if initial.status not in TERMINAL and task == 'shared' and not meta.get('cache_source_id'):
            raise ProcessingError('INVALID_JOB', 'Shared run has no source.')
        if initial.status not in TERMINAL and task in {'shared', 'cache_hit'} and meta.get('cache_source_id'):
            source = await self.get(UUID(meta['cache_source_id']))
            if source.document_id != initial.document_id or source.processing_fingerprint != initial.processing_fingerprint:
                raise ProcessingError('ARCHIVE_INTEGRITY_ERROR', 'Cache source identity differs.', 503)
            if source.status not in TERMINAL:
                # Do not occupy a processing thread while another run extracts.
                # Pending stream entry is retried by the existing takeover loop.
                raise ProcessingError('SHARED_SOURCE_PENDING', 'Shared source is still processing.', 503)
        run = await self.claim(run_id, token, task)
        if run.status in TERMINAL:
            await self.project_terminal(run, token)
            return
        if run.status == JobState.processing:
            await self.repo.mark_processing(run_id, owner_token=token)
        else:
            await self.repo.mark_summarizing(run_id, owner_token=token)
        # Errors from archive/index/Redis are deliberately outside the provider
        # exception handler: defer delivery, never mislabel storage failure.
        if run.worker_attempts > self.settings.max_worker_attempts:
            run = await self.fail(run, token, ProcessingError('WORKER_RETRIES_EXCEEDED', 'Processing attempts exceeded.'))
        elif task in {'extract', 'summary'} and run.processing_fingerprint != fingerprint(run.document_id, run.parser, self.settings):
            run = await self.fail(run, token, ProcessingError('PIPELINE_CONFIGURATION_CHANGED', 'Worker processing settings differ from the accepted run. Restore the original settings or submit a new run.'))
        elif task in {'shared', 'cache_hit'}:
            if not run.has_extraction:
                if source and source.has_extraction:
                    run = await self.save_extraction(run, token, await self.read_extraction(source), source_run=source)
                elif not source and task == 'cache_hit' and run.parser.value == 'vision_figures':
                    run = await self.fail(run, token, ProcessingError('VISION_CACHE_SOURCE_MISSING', 'Cached images lack an archived source run.'))
                elif not source and task == 'cache_hit':
                    raw = await self.repo.redis.get(self.repo.key(run_id, 'pages'))
                    run = await self.save_extraction(run, token, self.repo._decode_pages(raw, meta))
            if run.status in TERMINAL:
                pass
            elif source and source.status != JobState.completed:
                run = await self.fail(run, token, ProcessingError(source.error_code or 'PROCESSING_FAILED', source.error_message or 'Shared processing failed.', diagnostic=source.error_diagnostic))
            elif run.status not in TERMINAL:
                summary = await self.read_summary(source) if source else await self.repo.redis.get(self.repo.key(run_id, 'summary'))
                run = await self.complete(run, token, summary)
        else:
            if not run.has_extraction:
                with TemporaryDirectory(prefix='pdf-archive-worker-') as directory:
                    path = Path(directory) / 'source.pdf'
                    await self.archive.fetch_original(run.document_id, path)
                    try:
                        async def report_progress(page, pages_total, attempt=1, max_attempts=1):
                            try:
                                await self.repo.set_page_progress(run_id, page, pages_total,
                                    owner_token=token, attempt=attempt, max_attempts=max_attempts)
                            except ProcessingError as exc:
                                if exc.code == 'JOB_LEASE_LOST':
                                    raise
                                log.warning('Unable to update page progress for %s: %s', run_id, exc.code)
                            except RedisError:
                                # UI progress must not cause a paid extraction to repeat.
                                log.warning('Temporary Redis outage while reporting page progress for %s', run_id)
                        if run.parser.value == 'vision_figures':
                            # Recover validated page replies from earlier runs
                            # of the same document, including failed runs made
                            # by v0.12.4 before the shared page cache existed.
                            earlier_runs = []
                            async for manifest_key in self.archive.iter_manifest_keys(run.document_id):
                                if '/runs/' not in manifest_key:
                                    continue
                                candidate = RunManifest.model_validate_json((await self.archive._read(manifest_key)).data)
                                if (candidate.run_id != run.run_id and candidate.parser.value == 'vision_figures'
                                        and candidate.processing_fingerprint == run.processing_fingerprint):
                                    earlier_runs.append(candidate.run_id)

                            async def durable_page(page, png, validate):
                                await self.require_owner(run_id, token)
                                adapter = processor.parsers['vision_figures']
                                return await self.archive.durable_vision_page(
                                    run.document_id, run.run_id, page, png, self.settings,
                                    lambda: adapter.client.complete_vision_figures_page(
                                        png, page=page, max_tokens=self.settings.vision_figures_max_tokens_per_page),
                                    validate, prior_runs=earlier_runs,
                                    on_attempt=lambda attempt: report_progress(
                                        page, len_count[0], attempt, self.settings.vision_figures_page_attempts))
                            # Parser reports the page count before invoking the
                            # durable page callback. Save it for retry progress.
                            len_count = [0]
                            async def vision_progress(page, pages_total, attempt=1, max_attempts=1):
                                len_count[0] = pages_total
                                await report_progress(page, pages_total, attempt, max_attempts)
                            parsed = await processor.extract(run.parser.value, path,
                                page_response=durable_page, on_progress=vision_progress)
                        else:
                            parsed = await processor.extract(run.parser.value, path,
                                on_progress=report_progress)
                    except ProcessingError as exc:
                        run = await self.fail(run, token, exc)
                    except (RedisError, ConnectionError):
                        raise
                    except Exception:
                        log.error('Unexpected extraction failure for run %s', run_id)
                        run = await self.fail(run, token, ProcessingError('PROCESSING_FAILED', 'Unexpected document processing error.'))
                    else:
                        run = await self.save_extraction(run, token, parsed)
            if run.status not in TERMINAL:
                parsed = await self.read_extraction(run)
                await self.repo.store_extracted(run_id, parsed, owner_token=token)
                try:
                    if processor.summarizer is None:
                        raise ProcessingError('SUMMARIZER_NOT_CONFIGURED', 'Document summarizer is not configured.')
                    with span('document.summarize'):
                        summary = await processor.summarizer.summarize(parsed.pages)
                    if not isinstance(summary, str) or not summary.strip():
                        raise ProcessingError('EMPTY_SUMMARY', 'Summarization returned empty content.')
                except ProcessingError as exc:
                    run = await self.fail(run, token, exc)
                except (RedisError, ConnectionError):
                    raise
                except Exception:
                    log.error('Unexpected summary failure for run %s', run_id)
                    run = await self.fail(run, token, ProcessingError('PROCESSING_FAILED', 'Unexpected document processing error.'))
                else:
                    run = await self.complete(run, token, summary)
        await self.project_terminal(run, token)

    async def status(self, run_id):
        run = await self.get(run_id)
        summary = await self.read_summary(run) if run.status == JobState.completed else None
        error = SafeError(code=run.error_code or 'PROCESSING_FAILED', message=run.error_message or 'Processing failed.', diagnostic=run.error_diagnostic) if run.status in {JobState.partial, JobState.failed} else None
        status = run.status
        # A queued summary retry is coordinated by Redis until it acquires a
        # durable claim. Keep the existing frontend polling in that interval.
        if status == JobState.partial:
            try:
                meta = await self.repo.get_meta(run_id)
                if meta and meta.get('archive_retry_id') and meta['archive_retry_id'] != run.retry_id and meta.get('status') == 'summarizing':
                    status, error = JobState.summarizing, None
            except RedisError:
                pass
        progress = None
        if status == JobState.processing:
            try:
                meta = await self.repo.get_meta(run_id)
                progress = self.repo.parse_page_progress(meta)
            except RedisError:
                # Progress is optional: archive status remains available even if
                # Redis metadata is absent or temporarily unavailable.
                pass
        return DocumentStatus(id=run.run_id, filename=run.filename, parser=run.parser,
            status=status, page_count=run.page_count, page_progress=progress,
            summary=summary, cache=run.cache,
            trace_id=run.trace_id, upload_id=run.upload_id, error=error,
            created_at=run.created_at, updated_at=run.updated_at)

    async def pages(self, run_id):
        run = await self.get(run_id)
        return PageList(id=run_id, pages=encode_pages(await self.read_extraction(run)))

    async def retry_summary(self, run_id, queue):
        run = await self.get(run_id)
        if run.status != JobState.partial:
            raise ProcessingError('SUMMARY_RETRY_NOT_ALLOWED', 'Only partial results can retry summarization.', 409)
        # Redis may have expired entirely. Recreate coordination metadata only;
        # pages are already durable. Atomic admission rejects concurrent retry.
        meta = self.repo.prepare_meta(run_id, run.filename, run.parser.value)
        meta.update(archive_document_id=run.document_id, cache=run.cache,
                    upload_id=str(run.upload_id or ''), trace_id=run.trace_id or '', status='summarizing', archive_retry_id=uuid4().hex)
        script = """
        -- archive:retry_summary
        local state = redis.call('HGET', KEYS[1], 'status')
        local owner = redis.call('HGET', KEYS[1], 'owner_token')
        if (state and state ~= 'partial') or (owner and owner ~= '') then return 0 end
        local meta = cjson.decode(ARGV[1])
        redis.call('DEL', KEYS[1])
        for k,v in pairs(meta) do redis.call('HSET', KEYS[1], k, tostring(v)) end
        redis.call('XADD', KEYS[2], '*', 'document_id', ARGV[2], 'parser', meta.parser,
            'action', 'summary', 'traceparent', ARGV[3], 'upload_id', meta.upload_id)
        return 1
        """
        if await self.repo.redis.eval(script, 2, self.repo.key(run_id, 'meta'), queue.stream,
                                      json.dumps(meta), str(run_id), inject()) != 1:
            raise ProcessingError('SUMMARY_RETRY_NOT_ALLOWED', 'A retry is already active or the previous job has not finished.', 409)
