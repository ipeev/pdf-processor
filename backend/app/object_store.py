"""Small S3-compatible source store. Only original PDFs live here, never Redis.

The local implementation is used by offline tests; production explicitly builds
S3ObjectStore. Object keys are UUID-derived (never client filenames).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from uuid import UUID

import boto3
from boto3.exceptions import Boto3Error
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.errors import ProcessingError


def object_key(document_id: UUID) -> str:
    if not isinstance(document_id, UUID):
        raise ProcessingError("INVALID_DOCUMENT_ID", "Invalid document identifier.")
    return f"uploads/{document_id}.pdf"


class S3ObjectStore:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.lifecycle_days = settings.s3_lifecycle_days
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )

    async def prepare(self) -> None:
        """Create the one upload bucket and install a conservative expiration rule."""
        def setup() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                self.client.create_bucket(Bucket=self.bucket)
            # Only our uploads/ keys expire. A job still running after this many
            # days may lose its PDF; the tradeoff is documented and configurable.
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={"Rules": [{
                    "ID": "temporary-pdf-expiration",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "uploads/"},
                    "Expiration": {"Days": self.lifecycle_days},
                }]},
            )
            # Check the rule was accepted and can be read back.
            rules = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket)["Rules"]
            if not any(rule.get("ID") == "temporary-pdf-expiration" and
                       rule.get("Expiration", {}).get("Days") == self.lifecycle_days
                       for rule in rules):
                raise RuntimeError("S3 lifecycle rule was not persisted")
        try:
            await asyncio.to_thread(setup)
        except (ClientError, BotoCoreError, Boto3Error, OSError, RuntimeError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Could not initialize S3-compatible storage.", 503) from exc

    async def ready(self) -> None:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except (ClientError, BotoCoreError, Boto3Error, OSError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "S3-compatible storage is unavailable.", 503) from exc

    async def put(self, doc_id: UUID, path: Path) -> None:
        try:
            await asyncio.to_thread(self.client.upload_file, str(path), self.bucket,
                                    object_key(doc_id), ExtraArgs={"ContentType": "application/pdf"})
        except (ClientError, BotoCoreError, Boto3Error, OSError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Unable to upload PDF to object storage.", 503) from exc

    async def fetch(self, doc_id: UUID, target: Path) -> None:
        try:
            await asyncio.to_thread(self.client.download_file, self.bucket, object_key(doc_id), str(target))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ProcessingError("SOURCE_FILE_MISSING", "PDF source object is missing.") from exc
            raise ProcessingError("STORAGE_UNAVAILABLE", "Unable to retrieve PDF from object storage.", 503) from exc
        except (BotoCoreError, Boto3Error, OSError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Unable to retrieve PDF from object storage.", 503) from exc

    async def remove(self, doc_id: UUID) -> None:
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=object_key(doc_id))
        except (ClientError, BotoCoreError, Boto3Error, OSError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Unable to remove PDF from object storage.", 503) from exc


    async def statistics(self) -> dict:
        """Count temporary PDF objects via standard S3 pagination (bounded scan)."""
        def inspect() -> dict:
            count = 0
            total_bytes = 0
            continuation = None
            truncated = False
            for _ in range(10):
                request = {"Bucket": self.bucket, "Prefix": "uploads/", "MaxKeys": 1000}
                if continuation:
                    request["ContinuationToken"] = continuation
                response = self.client.list_objects_v2(**request)
                for item in response.get("Contents", []):
                    count += 1
                    total_bytes += max(0, int(item.get("Size", 0)))
                if not response.get("IsTruncated"):
                    truncated = False
                    break
                continuation = response.get("NextContinuationToken")
                truncated = True
                if not continuation:
                    break
            else:
                return {"objects": count, "bytes": total_bytes, "truncated": True}
            return {"objects": count, "bytes": total_bytes, "truncated": truncated}
        try:
            return await asyncio.to_thread(inspect)
        except (ClientError, BotoCoreError, Boto3Error, OSError, ValueError, TypeError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Unable to read object-storage statistics.", 503) from exc

    async def exists(self, doc_id: UUID) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=object_key(doc_id))
            return True
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ProcessingError("STORAGE_UNAVAILABLE", "Cannot inspect object storage.", 503) from exc
        except (BotoCoreError, Boto3Error, OSError) as exc:
            raise ProcessingError("STORAGE_UNAVAILABLE", "Cannot inspect object storage.", 503) from exc


class LocalTestObjectStore:
    """File-backed implementation for existing no-network unit tests only."""
    def __init__(self, directory: Path):
        self.directory = directory

    def source(self, doc_id: UUID) -> Path:
        return self.directory / f"{doc_id}.pdf"

    async def ready(self):
        self.directory.mkdir(parents=True, exist_ok=True)

    async def put(self, doc_id: UUID, path: Path):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.source(doc_id)
        if target != path:
            os.replace(path, target)

    async def fetch(self, doc_id: UUID, target: Path):
        source = self.source(doc_id)
        if not source.is_file():
            raise ProcessingError("SOURCE_FILE_MISSING", "PDF source file is not accessible.")
        await asyncio.to_thread(shutil.copyfile, source, target)

    async def remove(self, doc_id: UUID):
        self.source(doc_id).unlink(missing_ok=True)

    async def exists(self, doc_id: UUID):
        return self.source(doc_id).is_file()
