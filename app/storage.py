"""File storage for photographs. Bytes live on local disk by default, or in a Cloudflare R2 bucket.
Access is always mediated by the app."""

import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from starlette.responses import FileResponse, Response, StreamingResponse

from app.config import settings

VARIANTS = ("original", "display", "thumb")


def check(key):
    if not re.fullmatch(r"[a-zA-Z0-9_./-]+", key) or ".." in key or key.startswith("/"):
        raise ValueError("Invalid storage key")
    return key


# The photographer's card logo, always a PNG whatever was uploaded (app/routes/profile.py).
LOGO_KEY = "logo.png"


def photo_key(photo_id, variant):
    return f"photos/{photo_id}/{variant}.jpg"


class Local:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        return self.root / check(key)

    def put(self, key, data):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A unique scratch file per write, renamed into place, so readers never see half a file.
        handle, scratch = tempfile.mkstemp(dir=target.parent, prefix="." + target.name, suffix=".tmp")
        try:
            # mkstemp creates the file owner-only; stored files stay readable like any other.
            os.fchmod(handle, 0o644)
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(scratch, target)
        except BaseException:
            Path(scratch).unlink(missing_ok=True)
            raise

    def get(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def exists(self, key):
        return self.path(key).is_file()

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def response(self, key, media_type, filename):
        if not self.exists(key):
            return Response(status_code=404)
        return FileResponse(self.path(key), media_type=media_type, filename=filename)


class R2:
    def __init__(self, account_id, access_key_id, secret_access_key, bucket):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 4, "mode": "standard"},
            ),
        )

    def put(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=check(key), Body=data)

    def get(self, key):
        return self.open(key)["Body"].read()

    def open(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=check(key))
        except self.client.exceptions.NoSuchKey:
            raise KeyError(key) from None

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=check(key))
            return True
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=check(key))

    def response(self, key, media_type, filename):
        try:
            found = self.open(key)
        except KeyError:
            return Response(status_code=404)
        headers = {"Content-Length": str(found["ContentLength"])}
        if filename:
            headers["Content-Disposition"] = content_disposition(filename)
        return StreamingResponse(found["Body"].iter_chunks(256 * 1024), media_type=media_type, headers=headers)


@lru_cache
def backend():
    config = settings()
    if config.storage_backend == "r2":
        return R2(config.r2_account_id, config.r2_access_key_id, config.r2_secret_access_key, config.r2_bucket)
    return Local(config.storage_root)


def put(key, data):
    backend().put(key, data)


def get(key):
    return backend().get(key)


def exists(key):
    return backend().exists(key)


def delete(key):
    backend().delete(key)


def content_disposition(filename):
    """An attachment header that survives non-ASCII names, matching Starlette's FileResponse."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def response(key, media_type, filename=None):
    return backend().response(key, media_type, filename)


def delete_photo_files(photo_id, variants=VARIANTS):
    """Keys are derived from the id alone, so the photo row may already be gone."""
    for variant in variants:
        delete(photo_key(photo_id, variant))
