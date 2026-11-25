"""Permanent, content-addressed archive. Stage 2: storage primitives only.

No Redis dependency, no user-controlled filenames in keys, no lifecycle rule.
Conditional PUTs must be verified on the actual S3 implementation before the
pipeline starts using this store. ETags are opaque CAS tokens, not SHA-256.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.errors import ProcessingError
from app.object_store import S3ObjectStore

_SHA = re.compile(r"[0-9a-f]{64}")
_ARTIFACTS = {"document.md": "text/markdown; charset=utf-8",
              "pages.json": "application/json",
              "summary.md": "text/markdown; charset=utf-8",
              "figures.json": "application/json"}
_IO = (BotoCoreError, Boto3Error, OSError)


def _doc(document_id: str) -> str:
    if not isinstance(document_id, str) or not _SHA.fullmatch(document_id):
        raise ProcessingError("INVALID_DOCUMENT_ID", "Invalid archive document identifier.")
    return document_id


def original_key(document_id: str) -> str:
    return f"{_doc(document_id)}/original.pdf"


def document_metadata_key(document_id: str) -> str:
    return f"{_doc(document_id)}/metadata.json"


def run_key(document_id: str, run_id: UUID, name: str) -> str:
    if not isinstance(run_id, UUID):
        raise ProcessingError("INVALID_RUN_ID", "Invalid archive run identifier.")
    if name not in {*_ARTIFACTS, "metadata.json"}:
        raise ProcessingError("INVALID_ARTIFACT", "Invalid archive artifact name.")
    return f"{_doc(document_id)}/runs/{run_id}/{name}"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _status(exc: ClientError) -> int | None:
    return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def _missing(exc: ClientError) -> bool:
    return _code(exc) in {"404", "NoSuchKey", "NotFound"} or _status(exc) == 404


def _conflict(exc: ClientError) -> bool:
    return _code(exc) in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"} or _status(exc) in {409, 412}


def _unavailable() -> ProcessingError:
    return ProcessingError("ARCHIVE_UNAVAILABLE", "Permanent archive storage is unavailable.", 503)


def _integrity() -> ProcessingError:
    return ProcessingError("ARCHIVE_INTEGRITY_ERROR", "Archived object failed integrity verification.", 500)


def _exact_etag(value: str) -> bool:
    # A single strong S3 ETag, not a wildcard, list or weak validator.
    return isinstance(value, str) and re.fullmatch(r'"[^"*,\s]+"', value) is not None


def _etag(response: dict) -> str:
    value = response.get("ETag")
    if isinstance(value, str) and not value.startswith('"'):
        value = f'"{value}"'  # normalize servers that omit surrounding quotes
    if not _exact_etag(value):
        raise _integrity()
    return value


def _json(data: Any) -> bytes:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProcessingError("INVALID_ARCHIVE_DATA", "Archive metadata must be valid JSON.") from exc


@dataclass(frozen=True)
class Original:
    document_id: str
    key: str
    size_bytes: int
    etag: str
    created: bool


@dataclass(frozen=True)
class Artifact:
    data: bytes
    etag: str

    def json(self) -> Any:
        return json.loads(self.data)


class ArchiveStore:
    def __init__(self, settings: Settings, *, client=None):
        self.bucket = settings.s3_archive_bucket
        self.client = client if client is not None else S3ObjectStore(settings).client
        if self.bucket == settings.s3_bucket:
            raise ProcessingError("INVALID_ARCHIVE_BUCKET", "Archive and temporary buckets must be different.")

    async def prepare(self) -> None:
        """Create bucket if needed; refuse any configured lifecycle, never erase it."""
        def setup():
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if not _missing(exc):
                    raise
                try:
                    self.client.create_bucket(Bucket=self.bucket)
                except ClientError as create_exc:
                    if _code(create_exc) != "BucketAlreadyOwnedByYou":
                        raise
            self.client.head_bucket(Bucket=self.bucket)
            try:
                rules = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket).get("Rules", [])
            except ClientError as exc:
                if _code(exc) != "NoSuchLifecycleConfiguration":
                    raise
                rules = []
            if rules:
                raise ProcessingError("ARCHIVE_LIFECYCLE_UNSAFE", "Permanent archive bucket must not have lifecycle rules.", 503)
        try:
            await asyncio.to_thread(setup)
        except (ClientError, *_IO) as exc:
            raise _unavailable() from exc

    async def validate_lifecycle(self) -> None:
        """Read-only validation of the permanent bucket retention policy."""
        def check():
            try:
                rules = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket).get("Rules", [])
            except ClientError as exc:
                if _code(exc) != "NoSuchLifecycleConfiguration":
                    raise
                rules = []
            if rules:
                raise ProcessingError("ARCHIVE_LIFECYCLE_UNSAFE", "Permanent archive bucket must not have lifecycle rules.", 503)
        try:
            await asyncio.to_thread(check)
        except (ClientError, *_IO) as exc:
            raise _unavailable() from exc

    async def ready(self) -> None:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except (ClientError, *_IO) as exc:
            raise _unavailable() from exc

    def _put(self, key: str, body, content_type: str, *, expected_etag: str | None = None, **kwargs) -> str:
        if expected_etag is not None and not _exact_etag(expected_etag):
            raise ProcessingError("INVALID_ARCHIVE_ETAG", "An exact archive ETag is required.")
        condition = {"IfNoneMatch": "*"} if expected_etag is None else {"IfMatch": expected_etag}
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                              ContentType=content_type, **condition, **kwargs)
            return _etag(response)
        except ClientError as exc:
            if _conflict(exc):
                raise ProcessingError("ARCHIVE_CONFLICT", "Archive object changed or already exists.", 409) from exc
            raise _unavailable() from exc
        except _IO as exc:
            raise _unavailable() from exc

    def _get(self, key: str) -> dict:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _missing(exc) and _code(exc) != "NoSuchBucket":
                raise ProcessingError("ARCHIVE_NOT_FOUND", "Archive object does not exist.", 404) from exc
            raise _unavailable() from exc
        except _IO as exc:
            raise _unavailable() from exc

    def _read_original(self, document_id: str, output=None) -> tuple[int, str]:
        response = self._get(original_key(document_id))
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := body.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if output is not None:
                    output.write(chunk)
            if digest.hexdigest() != document_id or size != response.get("ContentLength"):
                raise _integrity()
            return size, _etag(response)
        finally:
            body.close()

    async def put_original(self, path: Path, *, expected_sha256: str | None = None) -> Original:
        """Hash staged bytes, then atomically create the single canonical original.

        Caller must own a stable staged file. Concurrent identical requests use
        the same key; on a precondition failure verify the existing stored bytes.
        """
        if expected_sha256 is not None:
            _doc(expected_sha256)

        def store():
            with path.open("rb") as source:
                digest = hashlib.sha256()
                size = 0
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                doc_id = digest.hexdigest()
                if expected_sha256 is not None and doc_id != expected_sha256:
                    raise _integrity()
                key = original_key(doc_id)
                # Snapshot the staged bytes into a private spool before PUT if
                # callers ever permit mutable inputs; for now staging owns them.
                for attempt in range(3):
                    source.seek(0)
                    try:
                        tag = self._put(key, source, "application/pdf", ContentLength=size,
                                        Metadata={"sha256": doc_id})
                    except ProcessingError as exc:
                        if exc.code != "ARCHIVE_CONFLICT":
                            raise
                        try:
                            actual_size, tag = self._read_original(doc_id)
                        except ProcessingError as missing:
                            if missing.code == "ARCHIVE_NOT_FOUND" and attempt < 2:
                                time.sleep(0.05 * (attempt + 1))
                                continue
                            raise
                        if actual_size != size:
                            raise _integrity()
                        return Original(doc_id, key, size, tag, False)
                    return Original(doc_id, key, size, tag, True)
                raise _unavailable()  # defensive; every loop path returns or raises
        try:
            return await asyncio.to_thread(store)
        except _IO as exc:
            raise _unavailable() from exc

    async def fetch_original(self, document_id: str, target: Path) -> None:
        """Stream and verify before atomic publication; preserve target on error."""
        _doc(document_id)

        def fetch():
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".archive-", delete=False) as output:
                    temp = Path(output.name)
                    self._read_original(document_id, output)
                os.replace(temp, target)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        try:
            await asyncio.to_thread(fetch)
        except _IO as exc:
            raise _unavailable() from exc

    async def _write(self, key: str, data: bytes, content_type: str, expected_etag: str | None = None) -> str:
        return await asyncio.to_thread(self._put, key, data, content_type, expected_etag=expected_etag)

    async def _read(self, key: str, *, max_bytes: int | None = None) -> Artifact:
        def read():
            response = self._get(key)
            body = response["Body"]
            try:
                if max_bytes is not None and response.get("ContentLength", max_bytes + 1) > max_bytes:
                    raise ProcessingError("ARCHIVE_BACKUP_LIMIT", "Archive object exceeds the backup object limit.", 413)
                data = body.read() if max_bytes is None else body.read(max_bytes + 1)
                if len(data) != response.get("ContentLength"):
                    raise _integrity()
                return Artifact(data, _etag(response))
            finally:
                body.close()
        try:
            return await asyncio.to_thread(read)
        except _IO as exc:
            raise _unavailable() from exc

    async def write_document_metadata(self, document_id: str, data: dict, *, expected_etag: str | None = None) -> str:
        return await self._write(document_metadata_key(document_id), _json(data), "application/json", expected_etag)

    async def read_document_metadata(self, document_id: str) -> Artifact:
        return await self._read(document_metadata_key(document_id))

    async def write_run_metadata(self, document_id: str, run_id: UUID, data: dict, *, expected_etag: str | None = None) -> str:
        return await self._write(run_key(document_id, run_id, "metadata.json"), _json(data), "application/json", expected_etag)

    async def read_run_metadata(self, document_id: str, run_id: UUID) -> Artifact:
        return await self._read(run_key(document_id, run_id, "metadata.json"))

    def _artifact_key(self, document_id: str, run_id: UUID, name: str, generation: str | None) -> str:
        base = run_key(document_id, run_id, name)
        if generation is None:
            return base
        if not re.fullmatch(r'[0-9a-f]{32}', generation):
            raise ProcessingError('INVALID_ARTIFACT', 'Invalid artifact generation.')
        return f'{base.rsplit("/", 1)[0]}/artifacts/{generation}/{name}'

    async def write_run_locator(self, document_id: str, run_id: UUID) -> None:
        _doc(document_id)
        if not isinstance(run_id, UUID):
            raise ProcessingError('INVALID_RUN_ID', 'Invalid archive run identifier.')
        key = f'run-ids/{run_id}.json'
        try:
            await self._write(key, _json({'document_id': document_id}), 'application/json')
        except ProcessingError as exc:
            if exc.code != 'ARCHIVE_CONFLICT' or await self.locate_run(run_id) != document_id:
                raise

    async def locate_run(self, run_id: UUID) -> str:
        if not isinstance(run_id, UUID):
            raise ProcessingError('INVALID_RUN_ID', 'Invalid archive run identifier.')
        artifact = await self._read(f'run-ids/{run_id}.json')
        try:
            return _doc(artifact.json()['document_id'])
        except (ValueError, KeyError, TypeError, ProcessingError) as exc:
            raise _integrity() from exc

    async def write_run_artifact(self, document_id: str, run_id: UUID, name: str, data: bytes, *, generation: str | None = None) -> str:
        """Artifacts are immutable at this stage; no unconditional overwrite."""
        if name not in _ARTIFACTS or not isinstance(data, bytes):
            raise ProcessingError("INVALID_ARTIFACT", "Invalid archive artifact.")
        return await self._write(self._artifact_key(document_id, run_id, name, generation), data, _ARTIFACTS[name])

    async def read_run_artifact(self, document_id: str, run_id: UUID, name: str, *, generation: str | None = None) -> Artifact:
        if name not in _ARTIFACTS:
            raise ProcessingError("INVALID_ARTIFACT", "Invalid archive artifact.")
        return await self._read(self._artifact_key(document_id, run_id, name, generation))

    def figure_key(self, document_id: str, run_id: UUID, generation: str, filename: str) -> str:
        if not isinstance(filename, str) or not re.fullmatch(r'p[0-9]{4}-figure-[A-Za-z0-9_-]{1,70}\.png', filename):
            raise ProcessingError('INVALID_FIGURE', 'Invalid figure asset filename.', 422)
        if not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation):
            raise ProcessingError('INVALID_ARTIFACT', 'Invalid artifact generation.')
        return f'{_doc(document_id)}/runs/{run_id}/artifacts/{generation}/images/{filename}'

    async def write_figure(self, document_id: str, run_id: UUID, generation: str, filename: str, data: bytes) -> None:
        if type(data) is not bytes or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ProcessingError('INVALID_FIGURE', 'Figure is not a valid PNG payload.')
        await self._write(self.figure_key(document_id, run_id, generation, filename), data, 'image/png')

    async def read_figure(self, document_id: str, run_id: UUID, generation: str, filename: str, *, max_bytes: int = 20_000_000) -> bytes:
        artifact = await self._read(self.figure_key(document_id, run_id, generation, filename), max_bytes=max_bytes)
        if not artifact.data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise _integrity()
        return artifact.data

    def vision_call_key(self, document_id: str, run_id: UUID, page: int, *, response: bool = False,
                        attempt: int = 1, failure: bool = False) -> str:
        if type(page) is not int or not 1 <= page <= 10000:
            raise ProcessingError('INVALID_ARTIFACT', 'Invalid vision page number.')
        if not isinstance(run_id, UUID):
            raise ProcessingError('INVALID_RUN_ID', 'Invalid run ID.')
        if type(attempt) is not int or not 1 <= attempt <= 3:
            raise ProcessingError('INVALID_ARTIFACT', 'Invalid vision attempt number.')
        suffix = '' if attempt == 1 else f'-a{attempt:02d}'
        kind = 'failures' if failure else 'responses' if response else 'admissions'
        return f'{_doc(document_id)}/runs/{run_id}/vision/{kind}/p{page:04d}{suffix}.json'

    def vision_page_cache_key(self, document_id: str, png: bytes, settings) -> str:
        # Same original PDF and exact model input, regardless of run ID.
        # The prompt version is part of the key, so future prompt changes
        # cannot use incompatible old responses.
        from app.parsers.vision_figures import PROMPT_VERSION
        descriptor = _json({'version': 1, 'png_sha256': hashlib.sha256(png).hexdigest(),
            'prompt': PROMPT_VERSION, 'model': settings.vision_figures_model,
            'max_tokens': settings.vision_figures_max_tokens_per_page})
        return f'{_doc(document_id)}/vision-cache/{hashlib.sha256(descriptor).hexdigest()}.json'

    async def durable_vision_response(self, document_id: str, run_id: UUID, page: int, invoke, *, attempt: int = 1):
        """Record each paid attempt before invocation; replay its actual outcome.

        Old attempt-one object names are preserved for compatibility. An
        admission without an outcome is UNCERTAIN and may have been billed.
        Never automatically repeat it, even when another attempt is allowed.
        """
        from app.vision_retry import known_failure
        from app.provider_diagnostics import sanitize_diagnostic
        result_key = self.vision_call_key(document_id, run_id, page, response=True, attempt=attempt)
        failure_key = self.vision_call_key(document_id, run_id, page, failure=True, attempt=attempt)
        admission_key = self.vision_call_key(document_id, run_id, page, attempt=attempt)

        async def replay():
            saved = await self.optional(result_key)
            if saved is not None:
                return saved.data.decode('utf-8')
            failure = await self.optional(failure_key)
            if failure is not None:
                record = failure.json()
                if type(record) is not dict or set(record) != {'code', 'message', 'diagnostic'}:
                    raise _integrity()
                raise ProcessingError(record['code'], record['message'],
                                      diagnostic=sanitize_diagnostic(record['diagnostic']))
            return None

        existing = await replay()
        if existing is not None:
            return existing
        try:
            await self._write(admission_key, _json({'version': 1, 'page': page, 'attempt': attempt}), 'application/json')
        except ProcessingError as exc:
            if exc.code != 'ARCHIVE_CONFLICT':
                raise
            existing = await replay()
            if existing is not None:
                return existing
            raise ProcessingError('VISION_CALL_OUTCOME_UNKNOWN',
                                  'Previous vision request may have reached the provider; no automatic repeat is permitted.') from exc
        try:
            raw = await invoke()
        except ProcessingError as exc:
            if known_failure(exc):
                # Provider sent a definite error/invalid response. Persist a
                # sanitized outcome so a worker redelivery can progress to the
                # next attempt rather than treating it as an unknown charge.
                await self._write(failure_key, _json({'code': exc.code, 'message': exc.message,
                    'diagnostic': sanitize_diagnostic(exc.diagnostic)}), 'application/json')
            raise
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 2_000_000:
            err = ProcessingError('VISION_RESPONSE_INVALID', 'Vision response exceeds the archive limit.')
            await self._write(failure_key, _json({'code': err.code, 'message': err.message,
                'diagnostic': {}}), 'application/json')
            raise err
        await self._write(result_key, raw.encode('utf-8'), 'application/json')
        return raw

    async def durable_vision_page(self, document_id, run_id, page, png, settings, invoke, validate,
                                  *, prior_runs=(), on_attempt=None):
        """Return only validated pages; cache across failed and later runs.

        A corrupt/malformed response is never published into the shared cache.
        Prior v0.12.4 recorded responses are eligible for on-demand backfill
        only after being validated against the current page image.
        """
        from app.vision_retry import known_failure, retry_delay
        from app.vision_debug import log_invalid_vision_response
        import logging
        log = logging.getLogger(__name__)
        key = self.vision_page_cache_key(document_id, png, settings)
        cached = await self.optional(key)
        if cached is not None:
            # Even stored entries must pass the current strict validator.
            try:
                result = await validate(cached.data.decode('utf-8'))
            except (ProcessingError, UnicodeError) as exc:
                raise _integrity() from exc
            log.info('Validated vision page cache hit page=%d', page,
                     extra={'event': 'vision.page.cache.hit', 'page': page})
            return result

        async def publish(raw, result):
            try:
                await self._write(key, raw.encode('utf-8'), 'application/json')
            except ProcessingError as exc:
                if exc.code != 'ARCHIVE_CONFLICT':
                    raise
                # Concurrent same-document run may have cached an equally
                # valid but different model reply; prefer the canonical copy.
                current = await self._read(key)
                return await validate(current.data.decode('utf-8'))
            return result

        for earlier_run in prior_runs:
            raw_copy = await self.optional(self.vision_call_key(document_id, earlier_run, page, response=True))
            if raw_copy is None:
                continue
            try:
                raw = raw_copy.data.decode('utf-8')
                result = await validate(raw)
            except (ProcessingError, UnicodeError):
                # Ignore old invalid replies; never poison the new cache.
                continue
            log.info('Reusing previously paid vision page page=%d', page,
                     extra={'event': 'vision.page.cache.backfill', 'page': page})
            return await publish(raw, result)

        for attempt in range(1, settings.vision_figures_page_attempts + 1):
            if on_attempt is not None:
                await on_attempt(attempt)
            raw = None
            try:
                raw = await self.durable_vision_response(document_id, run_id, page,
                    invoke, attempt=attempt)
                result = await validate(raw)
            except ProcessingError as exc:
                if raw is not None:
                    log_invalid_vision_response(settings, raw, page=page,
                                                attempt=attempt, exc=exc)
                if not known_failure(exc) or attempt == settings.vision_figures_page_attempts:
                    raise exc.with_context(attempt=attempt,
                        max_attempts=settings.vision_figures_page_attempts)
                log.warning('Retrying invalid vision page=%d attempt=%d/%d reason=%s',
                    page, attempt, settings.vision_figures_page_attempts, exc.code,
                    extra={'event': 'vision.page.retry', 'page': page, 'error_code': exc.code})
                await retry_delay(attempt)
                continue
            log.info('Validated vision page stored page=%d attempt=%d', page, attempt,
                     extra={'event': 'vision.page.validated', 'page': page})
            return await publish(raw, result)
        raise AssertionError('Exhausted vision attempts without a terminal outcome')

    async def iter_manifest_keys(self, document_id: str | None = None):
        """Yield only canonical document/run manifest keys, through all S3 pages."""
        prefix = '' if document_id is None else f'{_doc(document_id)}/'
        token = None
        pattern = re.compile(r'[0-9a-f]{64}/(?:runs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/)?metadata\.json')
        while True:
            args = {'Bucket': self.bucket, 'Prefix': prefix, 'MaxKeys': 1000}
            if token is not None:
                args['ContinuationToken'] = token
            try:
                page = await asyncio.to_thread(self.client.list_objects_v2, **args)
            except (ClientError, *_IO) as exc:
                raise _unavailable() from exc
            for entry in page.get('Contents', []):
                key = entry['Key']
                if pattern.fullmatch(key) and key.startswith(prefix):
                    yield key
            if not page.get('IsTruncated'):
                break
            next_token = page.get('NextContinuationToken')
            if not next_token or next_token == token:
                raise _integrity()
            token = next_token

    async def iter_keys(self, prefix=''):
        """Stream archive keys; callers must hold exclusive gate for deletion."""
        token = None
        while True:
            args = {'Bucket': self.bucket, 'Prefix': prefix, 'MaxKeys': 1000}
            if token is not None:
                args['ContinuationToken'] = token
            try:
                page = await asyncio.to_thread(self.client.list_objects_v2, **args)
            except (ClientError, *_IO) as exc:
                raise _unavailable() from exc
            for entry in page.get('Contents', []):
                key = entry['Key']
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise _integrity()
                yield key
            if not page.get('IsTruncated'):
                break
            next_token = page.get('NextContinuationToken')
            if not next_token or next_token == token:
                raise _integrity()
            token = next_token

    async def remove_key(self, key):
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
        except (ClientError, *_IO) as exc:
            raise _unavailable() from exc

    async def optional(self, key):
        try:
            return await self._read(key)
        except ProcessingError as exc:
            if exc.code == 'ARCHIVE_NOT_FOUND':
                return None
            raise

    async def deletion_journal(self):
        return await self.optional('maintenance/deletion.json')

    async def restore_pending(self):
        return await self.optional("maintenance/restore.json") is not None

    async def assert_no_restore(self):
        if await self.restore_pending():
            raise ProcessingError("ARCHIVE_RESTORE_PENDING", "Archive restore is incomplete; maintenance is required.", 503)

    async def assert_no_deletion(self):
        await self.assert_no_restore()
        if await self.deletion_journal() is not None:
            raise ProcessingError('ARCHIVE_DELETION_PENDING', 'An interrupted deletion is being reconciled. Retry after maintenance.', 503)

    async def document_revision_floor(self, document_id):
        item = await self.optional(f'tombstones/documents/{_doc(document_id)}.json')
        if item is None:
            return 0
        try:
            floor = item.json()['revision_floor']
            if type(floor) is not int or not 1 <= floor < 2**63-1:
                raise ValueError()
            return floor
        except (ValueError, KeyError, TypeError) as exc:
            raise _integrity() from exc

    async def run_deleted(self, run_id):
        if not isinstance(run_id, UUID):
            raise ProcessingError('INVALID_RUN_ID', 'Invalid archive run identifier.')
        return await self.optional(f'tombstones/runs/{run_id}.json') is not None

    async def mark_reconciliation_needed(self):
        try:
            await self._write('maintenance/reconcile-needed.json', b'{}', 'application/json')
        except ProcessingError as exc:
            if exc.code != 'ARCHIVE_CONFLICT':
                raise

    async def reconciliation_needed(self):
        return await self.optional('maintenance/reconcile-needed.json') is not None
