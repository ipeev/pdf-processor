"""Disposable, token-scoped archive data for no-cost real RustFS checks.

No broad deletes: the token deterministically identifies only probe objects.
The production code intentionally does not implement deletion yet.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5, NAMESPACE_URL

from app.archive_store import ArchiveStore, document_metadata_key, original_key, run_key
from app.config import get_settings
from app.object_store import S3ObjectStore

SAMPLE = next(p / 'samples' / 'basic_text.pdf' for p in Path(__file__).resolve().parents
              if (p / 'samples' / 'basic_text.pdf').is_file())
ARTIFACTS = {
    'document.md': '# Математика\n\n$$\\frac{1}{2}$$\n',
    'pages.json': '[{"page":1,"markdown":"α + β"}]',
    'summary.md': 'Résumé: δ ≥ 0.\n',
}


class Probe:
    def __init__(self, token: UUID, store: ArchiveStore, temporary: S3ObjectStore):
        self.token = token
        self.store = store
        self.temporary = temporary
        self.pdf = SAMPLE.read_bytes() + f'\n% archive-test-{token}\n'.encode()
        self.doc = hashlib.sha256(self.pdf).hexdigest()
        self.run = uuid5(NAMESPACE_URL, f'pdf-archive-probe:{token}:run1')
        self.other_run = uuid5(NAMESPACE_URL, f'pdf-archive-probe:{token}:run2')
        self.meta = {'probe': str(token), 'filename': 'first.pdf', 'revision': 1}

    def keys(self):
        yield original_key(self.doc)
        yield document_metadata_key(self.doc)
        for run in (self.run, self.other_run):
            for name in ('metadata.json', *ARTIFACTS):
                yield run_key(self.doc, run, name)

    async def create(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'first.pdf'
            source.write_bytes(self.pdf)
            original = await self.store.put_original(source, expected_sha256=self.doc)
            assert original.created and original.document_id == self.doc
            await self.temporary.put(self.token, source)
        await self.store.write_document_metadata(self.doc, self.meta)
        for run, filename in ((self.run, 'first.pdf'), (self.other_run, 'renamed.pdf')):
            await self.store.write_run_metadata(self.doc, run, {'filename': filename, 'revision': 1})
            for name, text in ARTIFACTS.items():
                await self.store.write_run_artifact(self.doc, run, name, text.encode('utf-8'))

    async def verify(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'restored.pdf'
            await self.store.fetch_original(self.doc, target)
            assert target.read_bytes() == self.pdf
        assert (await self.store.read_document_metadata(self.doc)).json() == self.meta
        for run, filename in ((self.run, 'first.pdf'), (self.other_run, 'renamed.pdf')):
            assert (await self.store.read_run_metadata(self.doc, run)).json()['filename'] == filename
            for name, text in ARTIFACTS.items():
                assert (await self.store.read_run_artifact(self.doc, run, name)).data == text.encode('utf-8')

    async def cleanup(self):
        await self.temporary.remove(self.token)
        for key in self.keys():
            await asyncio.to_thread(self.store.client.delete_object, Bucket=self.store.bucket, Key=key)


async def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit('Usage: archive_probe check | {create|verify|cleanup} UUID')
    action = sys.argv[1]
    settings = get_settings()
    temporary = S3ObjectStore(settings)
    store = ArchiveStore(settings, client=temporary.client)
    try:
        if action == 'check' and len(sys.argv) == 2:
            # Do not create a missing bucket here: setup must have initialized it.
            await temporary.ready()
            await store.ready()
            await store.prepare()  # verifies no archive lifecycle configuration
            print('PASS separate temporary and permanent RustFS buckets; archive has no lifecycle rules')
            return
        if len(sys.argv) != 3:
            raise SystemExit('Probe UUID required')
        probe = Probe(UUID(sys.argv[2]), store, temporary)
        if action == 'create':
            await probe.create()
        elif action == 'verify':
            await probe.verify()
        elif action == 'cleanup':
            await probe.cleanup()
        else:
            raise SystemExit('Unknown action')
    finally:
        temporary.client.close()


if __name__ == '__main__':
    asyncio.run(main())
