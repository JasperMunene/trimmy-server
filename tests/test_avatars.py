"""Profile pictures: upload, use the Google photo, remove; stored small and square, and shown on
the person's staff profiles."""
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

import app.modules.auth.avatar as avatar_module
from app.core.config import settings
from app.modules.auth.avatar import google_photo_url
from app.modules.business.models import Staff
from tests.test_business_setup_fixes import _business, _drain_background


def _image(fmt="JPEG", size=(2000, 1500)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (90, 140, 200)).save(out, fmt)
    return out.getvalue()


async def _upload(client, headers, data, name="me.jpg", ctype="image/jpeg"):
    return await client.put("/api/v1/auth/me/avatar", files={"photo": (name, data, ctype)}, headers=headers)


def _stored(url: str) -> Path:
    return Path(settings.MEDIA_ROOT) / url.removeprefix(settings.MEDIA_BASE_URL + "/")


@pytest.mark.asyncio
async def test_upload_is_stored_small_and_square(client, auth_headers):
    original = _image()
    resp = await _upload(client, auth_headers, original)
    assert resp.status_code == 200, resp.text
    url = resp.json()["avatar_url"]
    assert url.endswith(".jpg")
    stored = _stored(url).read_bytes()
    with Image.open(io.BytesIO(stored)) as img:
        assert (img.format, img.size) == ("JPEG", (512, 512))
    assert len(stored) < len(original)
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).json()["avatar_url"] == url


@pytest.mark.asyncio
async def test_png_and_replacing_deletes_the_old_file(client, auth_headers):
    first = (await _upload(client, auth_headers, _image("PNG"), "a.png", "image/png")).json()["avatar_url"]
    second = (await _upload(client, auth_headers, _image())).json()["avatar_url"]
    assert first != second  # a new URL each time (old ones are cached as immutable)
    await _drain_background()
    assert not _stored(first).exists() and _stored(second).exists()


@pytest.mark.asyncio
async def test_remove(client, auth_headers):
    url = (await _upload(client, auth_headers, _image())).json()["avatar_url"]
    resp = await client.delete("/api/v1/auth/me/avatar", headers=auth_headers)
    assert resp.status_code == 200 and resp.json()["avatar_url"] is None
    await _drain_background()
    assert not _stored(url).exists()


@pytest.mark.asyncio
async def test_rejects_non_images(client, auth_headers):
    resp = await _upload(client, auth_headers, b"<html>no</html>")
    assert resp.status_code == 400
    # Right magic bytes, but not a decodable picture.
    resp = await _upload(client, auth_headers, b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_shows_on_their_staff_profiles(client, auth_headers, test_user, db_session):
    biz_id = await _business(client, auth_headers)
    user_id = test_user.id
    db_session.add(Staff(business_id=uuid.UUID(biz_id), user_id=user_id, name="Me", role="owner"))
    await db_session.commit()

    url = (await _upload(client, auth_headers, _image())).json()["avatar_url"]
    db_session.expire_all()
    assert (await db_session.execute(select(Staff.avatar_url).where(Staff.user_id == user_id))).scalar_one() == url

    await client.delete("/api/v1/auth/me/avatar", headers=auth_headers)
    db_session.expire_all()
    assert (await db_session.execute(select(Staff.avatar_url).where(Staff.user_id == user_id))).scalar_one() is None


@pytest.mark.asyncio
async def test_use_google_photo(client, auth_headers, test_user, db_session, monkeypatch):
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert me["has_google_picture"] is False
    assert (await client.post("/api/v1/auth/me/avatar/google", headers=auth_headers)).status_code == 409

    test_user.google_picture_url = "https://lh3.googleusercontent.com/a/ACg8ocK=s96-c"
    await db_session.commit()
    fetched = []

    async def fake_download(url):
        fetched.append(url)
        return _image("PNG", (512, 512))

    monkeypatch.setattr(avatar_module, "_download", fake_download)
    resp = await client.post("/api/v1/auth/me/avatar/google", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_google_picture"] is True and resp.json()["avatar_url"]
    assert fetched == ["https://lh3.googleusercontent.com/a/ACg8ocK=s512-c"]  # asked for a big one


def test_only_google_photo_urls_are_fetched():
    assert google_photo_url("https://lh3.googleusercontent.com/a/x=s96-c") == "https://lh3.googleusercontent.com/a/x=s512-c"
    assert google_photo_url("https://lh3.googleusercontent.com/a/x") == "https://lh3.googleusercontent.com/a/x=s512-c"
    for bad in ("http://lh3.googleusercontent.com/a/x", "https://evil.example.com/x.jpg",
                "https://googleusercontent.com.evil.com/x", "", "file:///etc/passwd"):
        assert google_photo_url(bad) is None, bad


@pytest.mark.asyncio
async def test_google_sign_in_remembers_the_picture(db_session):
    from app.modules.auth.google import GoogleAuthService

    pic = "https://lh3.googleusercontent.com/a/new=s96-c"
    user = await GoogleAuthService(db_session)._find_or_link_user(
        {"sub": "g-123", "email": "pic@example.com", "given_name": "Pia", "picture": pic}
    )
    assert user.google_picture_url == pic
    # A later sign-in picks up a changed picture.
    newer = "https://lh3.googleusercontent.com/a/newer=s96-c"
    again = await GoogleAuthService(db_session)._find_or_link_user({"sub": "g-123", "email": "pic@example.com", "picture": newer})
    assert again.google_picture_url == newer
