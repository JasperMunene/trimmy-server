"""Object storage for uploaded files.

`get_storage()` returns the backend picked by STORAGE_BACKEND (R2 when R2_BUCKET
is set, otherwise local disk). Callers keep the object key next to the public
URL so the file can be deleted later.

Direct uploads: the app asks the API for a presigned PUT URL, uploads the file
straight to R2, then tells the API the key. The API checks the object (size,
magic bytes) before recording it. Only the R2 backend supports this.
"""
import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class DirectUploadUnsupported(RuntimeError):
    pass


@dataclass
class StoredObject:
    size: int
    content_type: Optional[str]


class Storage(Protocol):
    def public_url(self, key: str) -> str: ...

    async def save(self, key: str, data: bytes, content_type: str) -> str:
        """Store `data` under `key` and return its public URL."""

    async def delete(self, key: str) -> None:
        """Remove `key`. Missing keys are not an error."""

    async def presign_upload(self, key: str, content_type: str, size_bytes: int, expires: int) -> str:
        """A URL the client can PUT the file to directly."""

    async def stat(self, key: str) -> Optional[StoredObject]:
        """Size and type of `key`, or None if it doesn't exist."""

    async def read(self, key: str) -> bytes:
        """The whole object (photos up to MAX_PHOTO_BYTES, to shrink after a direct upload)."""

    async def read_head(self, key: str, length: int) -> bytes:
        """The first `length` bytes of `key`."""


class LocalStorage:
    """Writes under MEDIA_ROOT; main.py serves that directory at /media. Dev only."""

    def __init__(self, root: str, base_url: str):
        self.root = Path(root).resolve()
        self.base_url = base_url.rstrip("/")

    def public_url(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError(f"Invalid storage key: {key}")
        return path

    async def save(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)
        return self.public_url(key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def presign_upload(self, key: str, content_type: str, size_bytes: int, expires: int) -> str:
        raise DirectUploadUnsupported("Direct uploads need the R2 storage backend")

    async def stat(self, key: str) -> Optional[StoredObject]:
        path = self._path(key)
        if not path.exists():
            return None
        return StoredObject(size=path.stat().st_size, content_type=None)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def read_head(self, key: str, length: int) -> bytes:
        def read() -> bytes:
            with self._path(key).open("rb") as f:
                return f.read(length)

        return await asyncio.to_thread(read)


class R2Storage:
    """Cloudflare R2 through its S3-compatible API.

    The bucket must be publicly readable at R2_PUBLIC_URL (r2.dev or a custom
    domain), since the app loads photo URLs directly. For browser (web) uploads
    the bucket also needs a CORS rule allowing PUT from the web origin.
    """

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        missing = [
            name for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_URL")
            if not getattr(settings, name)
        ]
        if missing:
            raise RuntimeError(f"R2 storage is missing settings: {', '.join(missing)}")

        self.bucket = settings.R2_BUCKET
        self.base_url = settings.R2_PUBLIC_URL.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    def public_url(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    async def save(self, key: str, data: bytes, content_type: str) -> str:
        # boto3 is synchronous; keep it off the event loop.
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
        return self.public_url(key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def presign_upload(self, key: str, content_type: str, size_bytes: int, expires: int) -> str:
        # Content-Type and Content-Length are signed, so the upload must match
        # what the client declared. The object is still checked on completion.
        return await asyncio.to_thread(
            self.client.generate_presigned_url,
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": size_bytes,
            },
            ExpiresIn=expires,
        )

    async def stat(self, key: str) -> Optional[StoredObject]:
        from botocore.exceptions import ClientError

        try:
            head = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return StoredObject(size=head["ContentLength"], content_type=head.get("ContentType"))

    async def read(self, key: str) -> bytes:
        obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def read_head(self, key: str, length: int) -> bytes:
        obj = await asyncio.to_thread(
            self.client.get_object, Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}"
        )
        return await asyncio.to_thread(obj["Body"].read)


@lru_cache
def get_storage() -> Storage:
    backend = (settings.STORAGE_BACKEND or ("r2" if settings.R2_BUCKET else "local")).lower()
    if backend == "r2":
        return R2Storage()
    if backend == "local":
        return LocalStorage(settings.MEDIA_ROOT, settings.MEDIA_BASE_URL)
    raise RuntimeError(f"Unknown STORAGE_BACKEND: {settings.STORAGE_BACKEND}")


# ─── Image checks ────────────────────────────────────────────

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png"}

_IMAGE_SIGNATURES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


def sniff_image(data: bytes) -> Optional[str]:
    """The real content type from the file's magic bytes, or None if not JPEG/PNG.

    Client-declared types are only trusted after this agrees with them.
    """
    for signature, content_type in _IMAGE_SIGNATURES.items():
        if data.startswith(signature):
            return content_type
    return None
