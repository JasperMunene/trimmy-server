"""Profile pictures: upload one, use the picture on their Google account, or remove it.

Every picture is stored as one small square JPEG (app/core/images.py), so it loads fast on any
phone. The same picture shows on their staff profile at every business they work at."""
import logging
import re
import uuid as uuid_pkg
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.images import AVATAR_SIDE, ImageError, shrink_image
from app.core.storage import get_storage, sniff_image
from app.modules.auth.models import User

logger = logging.getLogger(__name__)

# Google profile photos are served from here; nothing else is fetched on a user's behalf.
GOOGLE_PHOTO_HOSTS = (".googleusercontent.com",)
# Google's URLs end in a size like "=s96-c": ask for one big enough to crop to AVATAR_SIDE.
_GOOGLE_SIZE = re.compile(r"=s\d+(-c)?$")


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def google_photo_url(url: str) -> Optional[str]:
    """A large version of a Google profile photo URL, or None if it isn't one."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(GOOGLE_PHOTO_HOSTS):
        return None
    return _GOOGLE_SIZE.sub("", url) + f"=s{AVATAR_SIDE}-c"


class AvatarService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upload(self, user: User, photo: UploadFile) -> User:
        data = await photo.read(settings.MAX_PHOTO_BYTES + 1)
        if not data:
            raise _error(400, "validation_error", "photo file is required")
        if len(data) > settings.MAX_PHOTO_BYTES:
            mb = settings.MAX_PHOTO_BYTES // (1024 * 1024)
            raise _error(413, "file_too_large", f"Photo exceeds the maximum upload size of {mb} MB")
        if sniff_image(data) is None:
            raise _error(400, "validation_error", "photo must be a JPEG or PNG image")
        return await self._set(user, data)

    async def from_google(self, user: User) -> User:
        url = google_photo_url(user.google_picture_url or "")
        if url is None:
            raise _error(409, "no_google_picture", "Sign in with Google first to use your Google photo")
        try:
            data = await _download(url)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"Couldn't fetch Google photo for user {user.id}: {e}")
            raise _error(502, "google_picture_unavailable", "Couldn't get your Google photo; please try again")
        return await self._set(user, data)

    async def remove(self, user: User) -> User:
        old = user.avatar_key
        user.avatar_url = user.avatar_key = None
        await self._sync_staff(user)
        await self.db.commit()
        await self.db.refresh(user)
        await _delete_later(old)
        return user

    async def _set(self, user: User, data: bytes) -> User:
        try:
            small = await shrink_image(data, AVATAR_SIDE, square=True, quality=82)
        except ImageError:
            raise _error(400, "validation_error", "photo must be a JPEG or PNG image")
        # A new key each time: the old URL stays cached for a year (immutable), so it can't be reused.
        key = f"users/{user.id}/avatar-{uuid_pkg.uuid4().hex[:12]}.jpg"
        url = await get_storage().save(key, small, "image/jpeg")
        old = user.avatar_key
        user.avatar_url, user.avatar_key = url, key
        await self._sync_staff(user)
        await self.db.commit()
        await self.db.refresh(user)
        await _delete_later(old)
        return user

    async def _sync_staff(self, user: User) -> None:
        """Their picture is their staff profile picture at every business they work at."""
        from app.modules.business.models import Staff

        await self.db.execute(update(Staff).where(Staff.user_id == user.id).values(avatar_url=user.avatar_url))


async def _download(url: str) -> bytes:
    """GET a photo, refusing anything over MAX_PHOTO_BYTES."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.MAX_PHOTO_BYTES:
                    raise ValueError("photo too large")
                chunks.append(chunk)
    return b"".join(chunks)


async def _delete_later(key: Optional[str]) -> None:
    if key:
        from app.modules.business.tasks import delete_stored_object

        await delete_stored_object.enqueue(key=key)
