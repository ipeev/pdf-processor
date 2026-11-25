"""Explicit idle archive audit, exact rebuild and optional checksummed backup.
No AI, no temporary bucket, no Redis data export, no filesystem extraction.
"""
import asyncio
import hashlib
import json
import re
import zipfile
from uuid import UUID

import httpx
from app.archive_gate import archive_gate, finish_before_unlock
from app.archive_monitor import DOC, RUN, ORIGINAL, DOCUMENT, RUN_MANIFEST, ARTIFACT, VISION_OBJECT, VISION_CACHE
from app.errors import ProcessingError
from app.persistent_pipeline import encode_pages
from app.reconciliation import rebuild

MAX_OBJECT = 128 * 1024**2
MAX_BACKUP = 8 * 1024**3
MAX_ENTRIES = 100_000
MANIFEST = 'manifest.json'
RESTORE = 'maintenance/restore.json'
LOCATOR = re.compile(rf'run-ids/{RUN}\.json')
TOMBSTONE = re.compile(rf'tombstones/(?:documents/{DOC}|runs/{RUN})\.json')


def invalid(message='Archive integrity check failed.'):
    return ProcessingError('ARCHIVE_INTEGRITY_ERROR', message, 503)


def content_type(key):
    if ORIGINAL.fullmatch(key):
        return 'application/pdf'
    if DOCUMENT.fullmatch(key) or RUN_MANIFEST.fullmatch(key) or LOCATOR.fullmatch(key) or TOMBSTONE.fullmatch(key):
        return 'application/json'
    if VISION_OBJECT.fullmatch(key) or VISION_CACHE.fullmatch(key):
        return 'application/json'
    if ARTIFACT.fullmatch(key):
        return ('image/png' if key.endswith('.png') else 'application/json' if key.endswith('.json')
                else 'text/markdown; charset=utf-8')
    raise invalid('Unknown archive object key; inspect before backup or restore.')


async def audit(m, *, check_index=True, restoring=False):
    """Must hold exclusive gate and have idle queue; verify committed content and exact projections."""
    a = m.archive
    if not restoring:
        await a.assert_no_deletion()
    await a.ready()
    await a.validate_lifecycle()
    docs, runs, referenced, preferred = {}, {}, set(), {}
    keys = {key async for key in a.iter_keys()}
    for key in sorted(keys):
        if DOCUMENT.fullmatch(key):
            doc_id = key.split('/')[0]
            doc = await m.p.documents.get(doc_id)
            size, _ = await asyncio.to_thread(a._read_original, doc_id)
            if size != doc.file_size_bytes:
                raise invalid('Original PDF size or SHA-256 mismatch.')
            docs[doc_id] = doc
            referenced.update((key, doc.s3_object_key))
            if doc.preferred_run_id:
                if str(doc.preferred_run_id) in preferred:
                    raise invalid("Preferred run is referenced by multiple documents.")
                preferred[str(doc.preferred_run_id)] = doc_id
    for key in sorted(keys):
        if not RUN_MANIFEST.fullmatch(key):
            continue
        doc_id, _, run_id, _ = key.split('/')
        uid = UUID(run_id)
        run = await m.p.runs.get(doc_id, uid)
        if doc_id not in docs or run_id in runs or await a.locate_run(uid) != doc_id:
            raise invalid('Run parent or locator is inconsistent.')
        if run.status not in ('completed', 'failed', 'partial'):
            raise invalid('Nonterminal run remains; reconcile the archive first.')
        if run_id in preferred and (preferred[run_id] != doc_id or run.status != 'completed' or docs[doc_id].page_count != run.page_count):
            raise invalid('Preferred run is inconsistent.')
        runs[run_id] = run
        referenced.update((key, f'run-ids/{uid}.json'))
        if run.has_extraction:
            parsed = await m.p.read_extraction(run)
            pages = encode_pages(parsed)
            markdown = '\n\n---\n\n'.join(f"# Page {p['page']}\n\n{p['markdown'].strip() or '_(Blank page)_'}" for p in pages) + '\n'
            stored = await a.read_run_artifact(doc_id, uid, 'document.md', generation=run.extraction_ref)
            if stored.data != markdown.encode():
                raise invalid('Archived Markdown differs from extracted pages.')
            for name in ('pages.json', 'document.md'):
                referenced.add(a._artifact_key(doc_id, uid, name, run.extraction_ref))
            if run.parser.value == 'vision_figures':
                figures = await m.p.read_figures(run, parsed)
                referenced.add(a._artifact_key(doc_id, uid, 'figures.json', run.extraction_ref))
                for fig in figures['figures']:
                    await m.p.read_figure(run, fig['filename'])
                    referenced.add(a.figure_key(doc_id, uid, run.extraction_ref, fig['filename']))
        if run.has_summary:
            await m.p.read_summary(run)
            referenced.add(a._artifact_key(doc_id, uid, 'summary.md', run.summary_ref))
    # Admitted provider calls and their saved responses belong to the run,
    # even when the extraction failed. Preserve them in exact backup/restore.
    for key in keys:
        if VISION_OBJECT.fullmatch(key):
            parent = key.split('/runs/', 1)[1].split('/', 1)[0]
            if parent not in runs or runs[parent].parser.value != 'vision_figures':
                raise invalid('Vision request record has no valid parent run.')
            referenced.add(key)
    # Shared page caches are scoped to a canonical document. They survive
    # deletion of an individual run and are included in exact backups.
    for key in keys:
        if VISION_CACHE.fullmatch(key):
            if key.split('/', 1)[0] not in docs:
                raise invalid('Vision page cache has no parent document.')
            referenced.add(key)
    if not set(preferred) <= runs.keys() or not referenced <= keys:
        raise invalid('Missing preferred run or referenced artifact.')
    for key in keys:
        if TOMBSTONE.fullmatch(key):
            obj = (await a._read(key)).json()
            if key.startswith('tombstones/documents/'):
                if type(obj) is not dict or set(obj) != {'revision_floor'}:
                    raise invalid('Invalid document tombstone.')
                await a.document_revision_floor(key.rsplit('/', 1)[1][:-5])
            elif type(obj) is not dict or set(obj) != {'document_id'} or not re.fullmatch(DOC, obj['document_id']):
                raise invalid('Invalid run tombstone.')
            referenced.add(key)
    if check_index:
        await m.indexes.ensure(create=False)
        for kind, records in (('documents', docs), ('runs', runs)):
            index, _ = m.indexes.names(kind)
            seen = set()
            async for identifier, doc_id, version in m.indexes.inventory(kind):
                item = records.get(identifier)
                if item is None or item.document_id != doc_id or item.revision != version:
                    raise invalid('Catalogue record identity or revision differs from RustFS; rebuild required.')
                data = await m.indexes.client.request('GET', f'/{index}/_doc/{identifier}')
                if data['_source'] != item.model_dump(mode='json'):
                    raise invalid('Catalogue record differs from RustFS; rebuild required.')
                seen.add(identifier)
            if seen != records.keys():
                raise invalid('Catalogue is missing archive records; rebuild required.')
    return {'documents': len(docs), 'runs': len(runs), 'objects': len(keys),
            'unreferenced_objects': len(keys - referenced - {'maintenance/reconcile-needed.json', RESTORE}),
            'index_verified': check_index}


async def exact_rebuild(m):
    """Delete/recreate only our two concrete catalogue indexes, never RustFS."""
    await audit(m, check_index=False)
    await m.archive.mark_reconciliation_needed()
    for kind in ('runs', 'documents'):
        name, _ = m.indexes.names(kind)
        try:
            await m.indexes.client.request('DELETE', f'/{name}', timeout=30.)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
    await rebuild(m.p.documents, m.p.runs)
    if getattr(m.p, 'search_index', None) is not None:
        await m.p.search_index.rebuild_all(m.p)
    result = await audit(m)
    await m.archive.remove_key('maintenance/reconcile-needed.json')
    return result


async def export_backup(m, output):
    await audit(m)
    a = m.archive
    if await a.reconciliation_needed():
        raise invalid('Reconcile dirty archive before backup.')
    entries, total = {}, 0
    # ZIP_STORED: archive bytes are not compressed; output may be non-seekable.
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        async for key in a.iter_keys():
            content_type(key)
            if key in entries or len(entries) >= MAX_ENTRIES:
                raise invalid('Backup object count limit exceeded.')
            data = (await a._read(key, max_bytes=MAX_OBJECT)).data
            total += len(data)
            if total > MAX_BACKUP:
                raise invalid('Backup payload size limit exceeded.')
            entries[key] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            z.writestr('objects/'+key, data)
        z.writestr(MANIFEST, json.dumps({'format': 1, 'bucket': a.bucket, 'objects': entries}, sort_keys=True).encode())
    return {'objects': len(entries), 'bytes': total}


def validate_backup(z, bucket):
    """Verify the entire package before any S3 write. No extract/extractall."""
    infos = z.infolist()
    names = {i.filename for i in infos}
    if len(infos) != len(names) or len(infos) > MAX_ENTRIES+1 or MANIFEST not in names:
        raise invalid('Invalid backup members or count.')
    if any(i.is_dir() or i.compress_type != zipfile.ZIP_STORED or i.file_size > MAX_OBJECT or i.flag_bits & 1 for i in infos):
        raise invalid('Unsupported backup member.')
    meta = json.loads(z.read(MANIFEST))
    if type(meta) is not dict or set(meta) != {'format','bucket','objects'} or type(meta['format']) is not int or meta['format'] != 1 or meta['bucket'] != bucket or type(meta['objects']) is not dict:
        raise invalid('Backup version/bucket mismatch.')
    entries = meta['objects']
    if names != {MANIFEST, *('objects/'+key for key in entries)}:
        raise invalid('Backup manifest/member mismatch.')
    total = 0
    for key, spec in entries.items():
        content_type(key)
        info = z.getinfo('objects/'+key)
        if type(spec) is not dict or set(spec) != {'size','sha256'} or type(spec['size']) is not int or spec['size'] != info.file_size:
            raise invalid('Backup size metadata mismatch.')
        total += info.file_size
        if total > MAX_BACKUP:
            raise invalid('Backup payload size limit exceeded.')
        data = z.read(info)
        digest = hashlib.sha256(data).hexdigest()
        if digest != spec['sha256'] or (ORIGINAL.fullmatch(key) and digest != key.split('/')[0]):
            raise invalid('Backup checksum mismatch.')
    return entries


async def import_backup(m, source):
    a = m.archive
    await a.assert_no_deletion()
    await a.prepare()
    if any([key async for key in a.iter_keys()]):
        raise invalid('Restore requires a freshly initialized empty permanent bucket.')
    await m.indexes.ensure()
    for kind in ('documents', 'runs'):
        async for _ in m.indexes.inventory(kind):
            raise invalid('Restore requires empty catalogue indexes.')
    # Parse and checksum all input before placing the durable blocking marker.
    with zipfile.ZipFile(source, 'r') as z:
        entries = await asyncio.to_thread(validate_backup, z, a.bucket)
        await a._write(RESTORE, b'{}', 'application/json')
        for key in entries:
            data = await asyncio.to_thread(z.read, 'objects/'+key)
            if ORIGINAL.fullmatch(key):
                await asyncio.to_thread(a._put, key, data, content_type(key), Metadata={'sha256': key.split('/')[0]})
            else:
                await a._write(key, data, content_type(key))
        await audit(m, check_index=False, restoring=True)
        await rebuild(m.p.documents, m.p.runs)
        result = await audit(m, restoring=True)
        await a.remove_key(RESTORE)
        return result


async def execute(m, action, stream=None):
    async def operation():
        await m.idle()
        if action == 'audit':
            return await audit(m)
        if action == 'rebuild':
            return await exact_rebuild(m)
        if action == 'backup':
            return await export_backup(m, stream)
        if action == 'restore':
            return await import_backup(m, stream)
        raise ValueError('Unknown operation')
    async with archive_gate(exclusive=True, path=m.path):
        return await finish_before_unlock(operation())
