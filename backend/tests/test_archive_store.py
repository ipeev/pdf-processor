"""Stage 2 contracts plus mandatory real RustFS tests. No AI or Redis required."""
from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

from app.archive_store import ArchiveStore, original_key, document_metadata_key, run_key
from app.config import Settings, get_settings
from app.errors import ProcessingError
from app.object_store import S3ObjectStore
from tests.archive_probe import Probe, ARTIFACTS


def error(code, status=404):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'probe')


class MemoryS3:
    """Thread-safe mock of S3 preconditions; not proof of RustFS semantics."""
    def __init__(self):
        self.buckets = set()
        self.rules = {}
        self.objects = {}
        self.lock = Lock()
        self.offline = False
        self.last_put = None

    def head_bucket(self, *, Bucket):
        if self.offline:
            raise EndpointConnectionError(endpoint_url='http://private-example')
        if Bucket not in self.buckets:
            raise error('NoSuchBucket')

    def create_bucket(self, *, Bucket):
        self.buckets.add(Bucket)

    def get_bucket_lifecycle_configuration(self, *, Bucket):
        if Bucket not in self.rules:
            raise error('NoSuchLifecycleConfiguration')
        return {'Rules': self.rules[Bucket]}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None, **kwargs):
        self.head_bucket(Bucket=Bucket)
        data = Body.read() if hasattr(Body, 'read') else Body
        assert kwargs.get('ContentLength', len(data)) == len(data)
        key = (Bucket, Key)
        with self.lock:
            previous = self.objects.get(key)
            if IfNoneMatch == '*' and previous is not None:
                raise error('PreconditionFailed', 412)
            if IfMatch is not None and (previous is None or IfMatch != previous[1]):
                raise error('PreconditionFailed', 412)
            assert IfNoneMatch == '*' or IfMatch is not None
            tag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
            self.objects[key] = (data, tag)
            self.last_put = (Bucket, Key, ContentType, kwargs)
            return {'ETag': tag}

    def get_object(self, *, Bucket, Key):
        self.head_bucket(Bucket=Bucket)
        with self.lock:
            try:
                data, tag = self.objects[Bucket, Key]
            except KeyError:
                raise error('NoSuchKey')
        return {'Body': BytesIO(data), 'ContentLength': len(data), 'ETag': tag}

    def delete_object(self, *, Bucket, Key):
        self.head_bucket(Bucket=Bucket)
        with self.lock:
            self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture
async def archive():
    config = Settings(_env_file=None, s3_bucket='pdf-uploads', s3_archive_bucket='pdf-documents')
    client = MemoryS3()
    store = ArchiveStore(config, client=client)
    await store.prepare()
    return store, client


def test_default_buckets_and_invalid_alias():
    settings = Settings(_env_file=None)
    assert settings.s3_archive_bucket == 'pdf-documents'
    assert settings.s3_bucket == 'pdf-uploads'
    with pytest.raises(ValidationError, match='S3_ARCHIVE_BUCKET'):
        Settings(_env_file=None, s3_bucket='same', s3_archive_bucket='same')


@pytest.mark.parametrize('value', ['../a', 'A' * 64, 'a' * 63, 'x' * 64, 'a' * 65, '', None])
def test_document_keys_reject_malformed_hash(value):
    with pytest.raises(ProcessingError, match='INVALID_DOCUMENT_ID'):
        original_key(value)


def test_run_keys_are_uuid_and_allowlisted():
    doc = '0' * 64
    run = uuid4()
    assert run_key(doc, run, 'summary.md') == f'{doc}/runs/{run}/summary.md'
    with pytest.raises(ProcessingError, match='INVALID_RUN_ID'):
        run_key(doc, '../escape', 'summary.md')
    with pytest.raises(ProcessingError, match='INVALID_ARTIFACT'):
        run_key(doc, run, '../escape')


@pytest.mark.asyncio
async def test_prepare_is_idempotent_and_rejects_any_archive_lifecycle(archive):
    store, client = archive
    await store.prepare()
    assert client.buckets == {'pdf-documents'}
    client.rules[store.bucket] = [{'ID': 'wrong', 'Expiration': {'Days': 1}}]
    with pytest.raises(ProcessingError, match='ARCHIVE_LIFECYCLE_UNSAFE'):
        await store.prepare()
    assert client.rules[store.bucket]  # never silently change policy


@pytest.mark.asyncio
async def test_original_dedup_renamed_and_modified(archive, tmp_path):
    store, client = archive
    source = tmp_path / 'first.pdf'
    source.write_bytes(b'%PDF-test\n')
    renamed = tmp_path / 'different.pdf'
    renamed.write_bytes(source.read_bytes())
    a, b = await store.put_original(source), await store.put_original(renamed)
    assert a.created and not b.created and a.document_id == b.document_id
    assert a.document_id == hashlib.sha256(source.read_bytes()).hexdigest()
    out = tmp_path / 'restored.pdf'
    await store.fetch_original(a.document_id, out)
    assert out.read_bytes() == source.read_bytes()
    source.write_bytes(b'%PDF-different\n')
    c = await store.put_original(source)
    assert c.created and c.document_id != a.document_id and len(client.objects) == 2


@pytest.mark.asyncio
async def test_concurrent_original_dedup(archive, tmp_path):
    store, client = archive
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'%PDF-concurrent')
    results = await asyncio.gather(*(store.put_original(source) for _ in range(8)))
    assert sum(r.created for r in results) == 1
    assert len({r.document_id for r in results}) == 1 and len(client.objects) == 1


@pytest.mark.asyncio
async def test_hash_mismatch_and_corrupted_original_are_rejected(archive, tmp_path):
    store, client = archive
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'%PDF-test')
    with pytest.raises(ProcessingError, match='ARCHIVE_INTEGRITY_ERROR'):
        await store.put_original(source, expected_sha256='0' * 64)
    assert not client.objects
    original = await store.put_original(source)
    client.objects[(store.bucket, original.key)] = (b'corrupt', original.etag)
    with pytest.raises(ProcessingError, match='ARCHIVE_INTEGRITY_ERROR'):
        await store.put_original(source)
    target = tmp_path / 'target.pdf'
    target.write_bytes(b'existing')
    with pytest.raises(ProcessingError, match='ARCHIVE_INTEGRITY_ERROR'):
        await store.fetch_original(original.document_id, target)
    assert target.read_bytes() == b'existing'
    assert not list(tmp_path.glob('.archive-*'))


@pytest.mark.asyncio
async def test_independent_runs_and_unicode(archive):
    store, _ = archive
    doc = 'a' * 64
    first, second = uuid4(), uuid4()
    for run in (first, second):
        await store.write_run_metadata(doc, run, {'filename': f'{run}.pdf'})
        for name, value in ARTIFACTS.items():
            await store.write_run_artifact(doc, run, name, value.encode())
            assert (await store.read_run_artifact(doc, run, name)).data == value.encode()
    assert (await store.read_run_metadata(doc, first)).json() != (await store.read_run_metadata(doc, second)).json()
    with pytest.raises(ProcessingError, match='ARCHIVE_CONFLICT'):
        await store.write_run_artifact(doc, first, 'summary.md', b'overwrite')
    assert (await store.read_run_artifact(doc, first, 'summary.md')).data == ARTIFACTS['summary.md'].encode()


async def check_cas(store, doc):
    old = await store.write_document_metadata(doc, {'revision': 1})
    with pytest.raises(ProcessingError, match='ARCHIVE_CONFLICT'):
        await store.write_document_metadata(doc, {'revision': 2})
    assert (await store.read_document_metadata(doc)).json() == {'revision': 1}
    results = await asyncio.gather(*(store.write_document_metadata(doc, {'revision': rev}, expected_etag=old)
                                     for rev in (2, 3)), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1, results
    assert sum(isinstance(r, ProcessingError) and r.code == 'ARCHIVE_CONFLICT' for r in results) == 1, results
    current = await store.read_document_metadata(doc)
    assert current.json()['revision'] in (2, 3) and current.etag != old
    with pytest.raises(ProcessingError, match='ARCHIVE_CONFLICT'):
        await store.write_document_metadata(doc, {'revision': 4}, expected_etag=old)
    assert (await store.read_document_metadata(doc)) == current


@pytest.mark.asyncio
async def test_cas_rejects_stale_and_concurrent_writers(archive):
    store, _ = archive
    await check_cas(store, 'b' * 64)


@pytest.mark.asyncio
async def test_outage_missing_and_invalid_data(archive):
    store, client = archive
    doc = 'c' * 64
    with pytest.raises(ProcessingError, match='ARCHIVE_NOT_FOUND'):
        await store.read_document_metadata(doc)
    with pytest.raises(ProcessingError, match='INVALID_ARCHIVE_DATA'):
        await store.write_document_metadata(doc, {'invalid': float('nan')})
    with pytest.raises(ProcessingError, match='INVALID_ARCHIVE_ETAG'):
        await store.write_document_metadata(doc, {}, expected_etag='*')
    client.offline = True
    with pytest.raises(ProcessingError, match='ARCHIVE_UNAVAILABLE') as err:
        await store.read_document_metadata(doc)
    assert err.value.http_status == 503 and 'private-example' not in str(err.value)
    with pytest.raises(ProcessingError, match='ARCHIVE_UNAVAILABLE'):
        await store.prepare()


@pytest.fixture
async def real_probe():
    settings = get_settings()
    temporary = S3ObjectStore(settings)
    store = ArchiveStore(settings, client=temporary.client)
    try:
        try:
            await temporary.ready()
        except ProcessingError:
            pytest.skip('RustFS unavailable outside Docker Compose')
        # Once temporary bucket is reachable, missing archive is a failure, not a skip.
        await store.ready()
        await store.prepare()
        probe = Probe(uuid4(), store, temporary)
        try:
            yield probe
        finally:
            await probe.cleanup()
    finally:
        temporary.client.close()


@pytest.mark.s3_integration
@pytest.mark.asyncio
async def test_real_archive_roundtrip_cleanup_and_lifecycle(real_probe):
    probe = real_probe
    await probe.create()
    await probe.verify()
    await probe.temporary.remove(probe.token)
    assert not await probe.temporary.exists(probe.token)
    await probe.verify()
    rules = probe.temporary.client.get_bucket_lifecycle_configuration(Bucket=probe.temporary.bucket)['Rules']
    assert any(r.get('Filter', {}).get('Prefix') == 'uploads/' and r.get('Expiration', {}).get('Days') == probe.temporary.lifecycle_days for r in rules)


@pytest.mark.s3_integration
@pytest.mark.asyncio
async def test_real_concurrent_original_and_renamed_dedup(real_probe, tmp_path):
    probe = real_probe
    files = [tmp_path / f'alias-{i}.pdf' for i in range(6)]
    for path in files:
        path.write_bytes(probe.pdf)
    result = await asyncio.gather(*(probe.store.put_original(path) for path in files))
    assert sum(row.created for row in result) == 1, result
    assert {row.document_id for row in result} == {probe.doc}
    found = probe.store.client.list_objects_v2(Bucket=probe.store.bucket, Prefix=f'{probe.doc}/')
    assert [o['Key'] for o in found['Contents']] == [original_key(probe.doc)]
    out = tmp_path / 'restore.pdf'
    await probe.store.fetch_original(probe.doc, out)
    assert out.read_bytes() == probe.pdf


@pytest.mark.s3_integration
@pytest.mark.asyncio
async def test_real_conditional_writes_are_not_silently_ignored(real_probe):
    await check_cas(real_probe.store, real_probe.doc)


@pytest.mark.asyncio
async def test_sdk_conditional_request_contract():
    """Exercise actual botocore parameter validation with network disabled."""
    import boto3
    from botocore.stub import Stubber
    settings = Settings(_env_file=None)
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test')
    store = ArchiveStore(settings, client=client)
    doc = 'f' * 64
    tag = '"abc123"'
    common = {'Bucket': store.bucket, 'Key': document_metadata_key(doc), 'ContentType': 'application/json'}
    try:
        with Stubber(client) as stub:
            stub.add_response('put_object', {'ETag': tag}, {**common, 'Body': b'{"revision":1}', 'IfNoneMatch': '*'})
            stub.add_response('put_object', {'ETag': '"new"'}, {**common, 'Body': b'{"revision":2}', 'IfMatch': tag})
            stub.add_client_error('put_object', service_error_code='PreconditionFailed', http_status_code=412,
                                  expected_params={**common, 'Body': b'{"revision":3}', 'IfMatch': tag})
            assert await store.write_document_metadata(doc, {'revision': 1}) == tag
            assert await store.write_document_metadata(doc, {'revision': 2}, expected_etag=tag) == '"new"'
            with pytest.raises(ProcessingError, match='ARCHIVE_CONFLICT'):
                await store.write_document_metadata(doc, {'revision': 3}, expected_etag=tag)
            stub.assert_no_pending_responses()
    finally:
        client.close()


@pytest.mark.parametrize('tag', ['"*"', 'W/"old"', '"a", "b"', 'unquoted', ''])
@pytest.mark.asyncio
async def test_invalid_etag_cannot_bypass_preconditions(archive, tag):
    store, client = archive
    with pytest.raises(ProcessingError, match='INVALID_ARCHIVE_ETAG'):
        await store.write_document_metadata('e' * 64, {}, expected_etag=tag)
    assert not client.objects
