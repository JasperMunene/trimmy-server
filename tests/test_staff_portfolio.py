"""Staff portfolio photos: the member manages their own, the owner manages anyone's;
customers see them on the business profile."""
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings
from app.modules.business.models import Business, Staff
from tests.test_business_setup_fixes import _drain_background, r2  # noqa: F401  (fixture)
from tests.test_business_setup_fixes import _business as _another_business
from tests.test_read_bookings import headers_for, make_user
from tests.test_staff_identity import _business, _invite_and_accept, member  # noqa: F401  (fixture)


async def _team(client, auth_headers, member_headers):
    """A business with one accepted stylist: (business id, staff id)."""
    biz_id = await _business(client, auth_headers)
    await _invite_and_accept(client, auth_headers, member_headers, biz_id)
    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    return biz_id, roster["staffId"]


def _image(fmt: str, size=(2400, 1800)) -> bytes:
    """A real photo-sized image (uploads are decoded and shrunk now)."""
    out = io.BytesIO()
    Image.new("RGB", size, (200, 120, 160)).save(out, fmt)
    return out.getvalue()


JPEG = _image("JPEG")
PNG = _image("PNG")


def _base(biz_id, staff_id):
    return f"/api/v1/businesses/{biz_id}/staff/{staff_id}/portfolio"


async def _upload(client, headers, biz_id, staff_id, body=JPEG, name="w.jpg", ctype="image/jpeg"):
    return await client.post(_base(biz_id, staff_id), files={"photo": (name, body, ctype)}, headers=headers)


@pytest.mark.asyncio
async def test_member_uploads_lists_and_deletes_their_own(client, auth_headers, member):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)

    first = await _upload(client, member_headers, biz_id, staff_id)
    assert first.status_code == 201, first.text
    second = (await _upload(client, member_headers, biz_id, staff_id, PNG, "w.png", "image/png")).json()
    photo = first.json()
    assert photo["staffId"] == staff_id
    assert photo["url"].startswith(f"{settings.MEDIA_BASE_URL}/businesses/{biz_id}/staff/{staff_id}/portfolio/")
    key = photo["url"].removeprefix(settings.MEDIA_BASE_URL + "/")
    # Stored shrunk: a JPEG no longer than 1600px a side.
    stored = (Path(settings.MEDIA_ROOT) / key).read_bytes()
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "JPEG" and max(img.size) == 1600
    assert second["url"].endswith(".jpg")  # PNGs become JPEGs too

    listed = (await client.get(_base(biz_id, staff_id), headers=member_headers)).json()
    assert [p["photoId"] for p in listed["photos"]] == [second["photoId"], photo["photoId"]]  # newest first
    assert listed["maxPhotos"] == settings.MAX_PORTFOLIO_PHOTOS

    assert (await client.delete(f"{_base(biz_id, staff_id)}/{photo['photoId']}", headers=member_headers)).status_code == 204
    await _drain_background()
    assert not (Path(settings.MEDIA_ROOT) / key).exists()
    assert [p["photoId"] for p in (await client.get(_base(biz_id, staff_id), headers=member_headers)).json()["photos"]] == [second["photoId"]]


@pytest.mark.asyncio
async def test_owner_manages_anyones_portfolio(client, auth_headers, member):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    photo = (await _upload(client, auth_headers, biz_id, staff_id)).json()
    assert (await client.delete(f"{_base(biz_id, staff_id)}/{photo['photoId']}", headers=auth_headers)).status_code == 204


@pytest.mark.asyncio
async def test_colleague_cannot_touch_someone_elses_portfolio(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    colleague = await make_user(db_session, "colleague@example.com")
    db_session.add(Staff(business_id=uuid.UUID(biz_id), user_id=colleague.id, name="Akinyi", role="staff"))
    await db_session.commit()

    resp = await _upload(client, headers_for(colleague), biz_id, staff_id)
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


@pytest.mark.asyncio
async def test_stranger_and_wrong_business_get_404(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    stranger = headers_for(await make_user(db_session, "stranger@example.com"))
    assert (await _upload(client, stranger, biz_id, staff_id)).status_code == 404

    other_biz = await _another_business(client, auth_headers)
    assert (await _upload(client, auth_headers, other_biz, staff_id)).status_code == 404


@pytest.mark.asyncio
async def test_rejects_non_images(client, auth_headers, member):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    resp = await _upload(client, member_headers, biz_id, staff_id, b"<html>nope</html>")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_portfolio_is_capped(client, auth_headers, member, monkeypatch):
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_PHOTOS", 2)
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    for _ in range(2):
        assert (await _upload(client, member_headers, biz_id, staff_id)).status_code == 201
    full = await _upload(client, member_headers, biz_id, staff_id)
    assert full.status_code == 409
    assert full.json()["error"] == "portfolio_full"


@pytest.mark.asyncio
async def test_profile_shows_uploaded_photos_newest_first(client, auth_headers, member, db_session):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    first = (await _upload(client, member_headers, biz_id, staff_id)).json()
    second = (await _upload(client, member_headers, biz_id, staff_id)).json()
    business = await db_session.get(Business, uuid.UUID(biz_id))
    business.status = "published"
    await db_session.commit()

    (stylist,) = [s for s in (await client.get(f"/api/v1/businesses/{biz_id}/profile")).json()["staff"] if s["staffId"] == staff_id]
    assert stylist["portfolio"] == [second["url"], first["url"]]


@pytest.mark.asyncio
async def test_removing_member_deletes_their_portfolio_files(client, auth_headers, member):
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    photo = (await _upload(client, member_headers, biz_id, staff_id)).json()
    path = Path(settings.MEDIA_ROOT) / photo["url"].removeprefix(settings.MEDIA_BASE_URL + "/")
    assert path.exists()

    (roster,) = (await client.get(f"/api/v1/businesses/{biz_id}/team/members", headers=auth_headers)).json()["members"]
    assert (await client.delete(f"/api/v1/businesses/{biz_id}/team/members/{roster['invitationId']}", headers=auth_headers)).status_code == 204
    await _drain_background()
    assert not path.exists()


# ─── Direct upload (fake R2) ─────────────────────────────────


@pytest.mark.asyncio
async def test_direct_upload_flow(client, auth_headers, member, r2):  # noqa: F811
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    grant = await client.post(
        f"{_base(biz_id, staff_id)}/upload-url", json={"contentType": "image/jpeg", "sizeBytes": len(JPEG)}, headers=member_headers,
    )
    assert grant.status_code == 200, grant.text
    key = grant.json()["key"]
    assert key.startswith(f"businesses/{biz_id}/staff/{staff_id}/portfolio/") and key.endswith(".jpg")

    r2.objects[key] = JPEG  # the app's PUT
    done = await client.post(f"{_base(biz_id, staff_id)}/complete", json={"key": key}, headers=member_headers)
    assert done.status_code == 201, done.text
    assert done.json()["url"] == f"https://pub.example.r2.dev/{key}"
    with Image.open(io.BytesIO(r2.objects[key])) as img:
        assert max(img.size) == 1600  # replaced by the shrunk copy
    again = await client.post(f"{_base(biz_id, staff_id)}/complete", json={"key": key}, headers=member_headers)
    assert again.json()["photoId"] == done.json()["photoId"]  # a retried completion is the same photo


@pytest.mark.asyncio
async def test_complete_rejects_keys_for_other_staff_and_bad_files(client, auth_headers, member, r2):  # noqa: F811
    _, member_headers = member
    biz_id, staff_id = await _team(client, auth_headers, member_headers)
    business_photo_key = f"businesses/{biz_id}/photos/{uuid.uuid4()}.jpg"
    r2.objects[business_photo_key] = JPEG
    other_staff_key = f"businesses/{biz_id}/staff/{uuid.uuid4()}/portfolio/{uuid.uuid4()}.jpg"
    r2.objects[other_staff_key] = JPEG
    for key in (business_photo_key, other_staff_key, "anything.jpg"):
        resp = await client.post(f"{_base(biz_id, staff_id)}/complete", json={"key": key}, headers=member_headers)
        assert resp.status_code == 400, key

    grant = (await client.post(
        f"{_base(biz_id, staff_id)}/upload-url", json={"contentType": "image/jpeg", "sizeBytes": len(JPEG)}, headers=member_headers,
    )).json()
    r2.objects[grant["key"]] = PNG  # a PNG under a .jpg key
    resp = await client.post(f"{_base(biz_id, staff_id)}/complete", json={"key": grant["key"]}, headers=member_headers)
    assert resp.status_code == 400
    assert grant["key"] in r2.deleted
