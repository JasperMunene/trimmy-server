import asyncio
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.modules.business.service as business_service
import app.modules.business.tasks as business_tasks
from app.core.config import settings
from app.core.storage import StoredObject
from app.modules.business.models import PaymentDestination, Staff, TeamInvitation

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_phone_counter = iter(range(10_000_000, 99_999_999))


async def _business(client: AsyncClient, headers: dict) -> str:
    resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Glow",
            "categories": ["hair_salon"],
            "phone": f"+2547{next(_phone_counter)}",
            "location": {"address": "Kilimani", "lat": -1.29, "lng": 36.78},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["businessId"]


async def _service(client, headers, biz_id, name="Cut") -> str:
    cat = await client.post(f"/api/v1/businesses/{biz_id}/service-categories", json={"name": f"Cat {name}"}, headers=headers)
    svc = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={"categoryId": cat.json()["categoryId"], "name": name, "durationMinutes": 30, "price": {"amount": 500, "currency": "KES"}},
        headers=headers,
    )
    assert svc.status_code == 201, svc.text
    return svc.json()["serviceId"]


async def _drain_background():
    # Photo deletes run via the worker task's in-process fallback in tests.
    await asyncio.sleep(0.05)


# ─── GET /businesses/{id}: camelCase all the way down ────────


@pytest.mark.asyncio
async def test_get_business_service_categories_are_camel_case(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    await _service(client, auth_headers, biz_id)

    data = (await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)).json()
    (cat,) = data["serviceCategories"]
    assert {"categoryId", "businessId", "name", "createdAt", "services"} <= cat.keys()
    (svc,) = cat["services"]
    assert {"serviceId", "categoryId", "durationMinutes", "price"} <= svc.keys()
    assert not any("_" in k for k in [*cat, *svc])


# ─── Photos: multipart through the API (local storage in tests) ─


@pytest.mark.asyncio
async def test_multipart_upload_stores_file(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("x.png", PNG, "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    photo = resp.json()
    assert photo["url"].startswith(settings.MEDIA_BASE_URL + f"/businesses/{biz_id}/photos/")
    assert photo["url"].endswith(".png")
    assert photo["isCover"] is True  # first photo is always the cover

    key = photo["url"].removeprefix(settings.MEDIA_BASE_URL + "/")
    assert (Path(settings.MEDIA_ROOT) / key).read_bytes() == PNG
    served = await client.get(f"/media/{key}")
    assert served.status_code == 200 and served.content == PNG


@pytest.mark.asyncio
async def test_multipart_rejects_non_image(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("x.jpg", b"<html>not an image</html>", "image/jpeg")},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_multipart_rejects_too_large(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "MAX_PHOTO_BYTES", 32)
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("x.jpg", JPEG, "image/jpeg")},
        headers=auth_headers,
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "file_too_large"


@pytest.mark.asyncio
async def test_new_cover_replaces_old_and_delete_removes_file(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    first = (await client.post(f"/api/v1/businesses/{biz_id}/photos", files={"photo": ("a.jpg", JPEG, "image/jpeg")}, headers=auth_headers)).json()
    second = (await client.post(
        f"/api/v1/businesses/{biz_id}/photos", files={"photo": ("b.jpg", JPEG, "image/jpeg")},
        data={"isCover": "true"}, headers=auth_headers,
    )).json()

    photos = {p["photoId"]: p["isCover"] for p in (await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)).json()["photos"]}
    assert photos == {first["photoId"]: False, second["photoId"]: True}

    path = Path(settings.MEDIA_ROOT) / second["url"].removeprefix(settings.MEDIA_BASE_URL + "/")
    assert path.exists()
    assert (await client.delete(f"/api/v1/businesses/{biz_id}/photos/{second['photoId']}", headers=auth_headers)).status_code == 204
    await _drain_background()
    assert not path.exists()


@pytest.mark.asyncio
async def test_upload_url_unavailable_with_local_storage(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos/upload-url",
        json={"contentType": "image/jpeg", "sizeBytes": 1000},
        headers=auth_headers,
    )
    assert resp.status_code == 501
    assert resp.json()["error"] == "direct_upload_unavailable"


# ─── Photos: presigned direct upload (fake R2) ───────────────


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.presigned: list[tuple] = []

    def public_url(self, key):
        return f"https://pub.example.r2.dev/{key}"

    async def save(self, key, data, content_type):
        self.objects[key] = data
        return self.public_url(key)

    async def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def presign_upload(self, key, content_type, size_bytes, expires):
        self.presigned.append((key, content_type, size_bytes, expires))
        return f"https://acct.r2.cloudflarestorage.com/bucket/{key}?X-Amz-Signature=sig"

    async def stat(self, key):
        data = self.objects.get(key)
        return None if data is None else StoredObject(size=len(data), content_type=None)

    async def read_head(self, key, length):
        return self.objects[key][:length]


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    monkeypatch.setattr(business_service, "get_storage", lambda: fake)
    monkeypatch.setattr(business_tasks, "get_storage", lambda: fake)
    return fake


async def _upload_url(client, headers, biz_id, content_type="image/jpeg", size=len(JPEG)):
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos/upload-url",
        json={"contentType": content_type, "sizeBytes": size},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_direct_upload_flow(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, biz_id)
    assert grant["method"] == "PUT"
    assert grant["headers"] == {"Content-Type": "image/jpeg"}
    assert grant["key"].startswith(f"businesses/{biz_id}/photos/") and grant["key"].endswith(".jpg")
    assert grant["expiresIn"] == settings.UPLOAD_URL_EXPIRE_SECONDS
    assert r2.presigned == [(grant["key"], "image/jpeg", len(JPEG), settings.UPLOAD_URL_EXPIRE_SECONDS)]

    r2.objects[grant["key"]] = JPEG  # the app's PUT
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"], "isCover": True}, headers=auth_headers,
    )
    assert resp.status_code == 201
    photo = resp.json()
    assert photo["url"] == f"https://pub.example.r2.dev/{grant['key']}"
    assert photo["isCover"] is True
    assert grant["key"].endswith(f"{photo['photoId']}.jpg")

    # Completing twice (e.g. a retry) returns the same photo.
    again = await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"]}, headers=auth_headers)
    assert again.status_code == 201 and again.json()["photoId"] == photo["photoId"]


@pytest.mark.asyncio
async def test_upload_url_rejects_oversize(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos/upload-url",
        json={"contentType": "image/jpeg", "sizeBytes": settings.MAX_PHOTO_BYTES + 1},
        headers=auth_headers,
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_url_rejects_other_types(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos/upload-url",
        json={"contentType": "image/gif", "sizeBytes": 100},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_complete_rejects_foreign_or_forged_keys(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    other_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, other_id)
    r2.objects[grant["key"]] = JPEG
    for key in (grant["key"], f"businesses/{biz_id}/photos/../../secret.jpg", "anything.jpg"):
        resp = await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": key}, headers=auth_headers)
        assert resp.status_code == 400, key


@pytest.mark.asyncio
async def test_complete_before_upload(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, biz_id)
    resp = await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"]}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["error"] == "upload_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<svg>not a jpeg</svg>", PNG])  # junk, or a PNG under a .jpg key
async def test_complete_rejects_and_deletes_bad_file(client, auth_headers, r2, body):
    biz_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, biz_id)
    r2.objects[grant["key"]] = body
    resp = await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"]}, headers=auth_headers)
    assert resp.status_code == 400
    assert grant["key"] in r2.deleted


@pytest.mark.asyncio
async def test_complete_rejects_and_deletes_oversize_file(client, auth_headers, r2, monkeypatch):
    biz_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, biz_id)
    monkeypatch.setattr(settings, "MAX_PHOTO_BYTES", 16)
    r2.objects[grant["key"]] = JPEG
    resp = await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"]}, headers=auth_headers)
    assert resp.status_code == 413
    assert grant["key"] in r2.deleted


@pytest.mark.asyncio
async def test_delete_photo_removes_object(client, auth_headers, r2):
    biz_id = await _business(client, auth_headers)
    grant = await _upload_url(client, auth_headers, biz_id)
    r2.objects[grant["key"]] = JPEG
    photo = (await client.post(f"/api/v1/businesses/{biz_id}/photos/complete", json={"key": grant["key"]}, headers=auth_headers)).json()
    await client.delete(f"/api/v1/businesses/{biz_id}/photos/{photo['photoId']}", headers=auth_headers)
    await _drain_background()
    assert r2.deleted == [grant["key"]]


# ─── Working hours ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("weekly", [
    {"funday": {"open": "09:00", "close": "17:00"}},
    {"monday": {"open": "25:99", "close": "26:00"}},
    {"monday": {"open": "9:00", "close": "17:00"}},
    {"monday": {"open": "09:00"}},
    {"monday": {"open": "09:00", "close": "17:00", "extra": 1}},
])
async def test_working_hours_rejects_malformed(client, auth_headers, weekly):
    biz_id = await _business(client, auth_headers)
    resp = await client.put(f"/api/v1/businesses/{biz_id}/working-hours", json={"weeklyHours": weekly}, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_working_hours_fills_all_days(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/working-hours",
        json={"weeklyHours": {"monday": {"open": "09:00", "close": "24:00"}}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    hours = resp.json()["weeklyHours"]
    assert hours["monday"] == {"open": "09:00", "close": "24:00"}
    assert [d for d, h in hours.items() if h is None] == ["tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ─── Deposit: percent + selected services ────────────────────


def _policies(deposit):
    return {
        "deposit": deposit,
        "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50},
        "noShow": {"feePercent": 100},
    }


@pytest.mark.asyncio
async def test_percent_deposit_uses_percent_field(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json=_policies({"required": True, "type": "percent", "percent": 30, "appliesTo": "all_services"}),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    deposit = resp.json()["deposit"]
    assert deposit["percent"] == 30
    assert deposit["amount"] is None
    assert deposit["serviceIds"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deposit", [
    {"required": True, "type": "percent", "appliesTo": "all_services"},
    {"required": True, "type": "fixed", "amount": {"amount": 0, "currency": "KES"}, "appliesTo": "all_services"},
    {"required": True, "type": "fixed", "amount": {"amount": 200, "currency": "KES"}, "appliesTo": "selected_services"},
])
async def test_required_deposit_needs_its_fields(client, auth_headers, deposit):
    biz_id = await _business(client, auth_headers)
    resp = await client.put(f"/api/v1/businesses/{biz_id}/policies", json=_policies(deposit), headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_selected_services_deposit(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    keep = await _service(client, auth_headers, biz_id, "Keep")
    drop = await _service(client, auth_headers, biz_id, "Drop")
    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json=_policies({
            "required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"},
            "appliesTo": "selected_services", "serviceIds": [keep, drop, keep],
        }),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deposit"]["serviceIds"] == [keep, drop]

    await client.delete(f"/api/v1/businesses/{biz_id}/services/{drop}", headers=auth_headers)
    draft = (await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)).json()
    assert draft["policies"]["deposit"]["serviceIds"] == [keep]


@pytest.mark.asyncio
async def test_selected_services_rejects_other_business_services(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    other_id = await _business(client, auth_headers)
    foreign = await _service(client, auth_headers, other_id)
    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json=_policies({
            "required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"},
            "appliesTo": "selected_services", "serviceIds": [foreign],
        }),
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert foreign in resp.json()["message"]


# ─── Payment destination ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"type": "mpesa_till", "tillNumber": "12"},
    {"type": "mpesa_till", "tillNumber": "12345678"},
    {"type": "mpesa_till", "tillNumber": "12a456"},
    {"type": "mpesa_paybill", "paybillNumber": "1234567", "paybillAccountNumber": "ACC"},
    {"type": "mpesa_paybill", "paybillNumber": "400200", "paybillAccountNumber": "   "},
    {"type": "bank_account", "bankName": "Equity", "bankShortcode": "24", "bankAccountNumber": "1234567890"},
    {"type": "bank_account", "bankName": "Equity", "bankShortcode": "247247", "bankAccountNumber": "12"},
])
async def test_payment_destination_formats(client, auth_headers, body):
    biz_id = await _business(client, auth_headers)
    resp = await client.put(f"/api/v1/businesses/{biz_id}/payment-destination", json=body, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_payment_destination_normalises_and_clears_old_type(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={"type": "bank_account", "bankName": "Equity Bank Kenya", "bankShortcode": "247247", "bankAccountNumber": "1234 5678 90"},
        headers=auth_headers,
    )
    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={"type": "mpesa_till", "tillNumber": "123 456"},
        headers=auth_headers,
    )
    data = resp.json()
    assert data["tillNumber"] == "123456"
    assert data["bankName"] is None and data["bankAccountNumber"] is None


@pytest.mark.asyncio
async def test_payment_destination_options(client):
    resp = await client.get("/api/v1/payment-destinations/options")
    assert resp.status_code == 200
    data = resp.json()
    assert [t["type"] for t in data["types"]] == ["mpesa_till", "mpesa_paybill", "bank_account"]
    till = data["types"][0]["fields"][0]
    assert till["name"] == "tillNumber" and till["pattern"] == r"^\d{5,7}$"
    assert {"name": "Equity Bank Kenya", "shortcode": "247247"} in data["banks"]


@pytest.mark.asyncio
async def test_publish_requires_verified_destination(client, auth_headers, db_session):
    biz_id = await _business(client, auth_headers)
    await client.put(f"/api/v1/businesses/{biz_id}/working-hours", json={"weeklyHours": {"monday": {"open": "09:00", "close": "17:00"}}}, headers=auth_headers)
    await _service(client, auth_headers, biz_id)
    await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json=_policies({"required": False, "type": "fixed", "appliesTo": "all_services"}),
        headers=auth_headers,
    )
    await client.put(f"/api/v1/businesses/{biz_id}/payment-destination", json={"type": "mpesa_till", "tillNumber": "123456"}, headers=auth_headers)

    pd = (await db_session.execute(select(PaymentDestination))).scalar_one()
    pd.verification_status = "pending"
    await db_session.commit()

    resp = await client.patch(f"/api/v1/businesses/{biz_id}/publish", json={"visible": True}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"] == "incomplete_setup"


# ─── Team members ────────────────────────────────────────────


def _member(**overrides):
    body = {"name": "Wanjiku", "phone": "+254712000001", "role": "staff", "commissionPercent": 40, "workingDays": None}
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_team_member_crud(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    base = f"/api/v1/businesses/{biz_id}/team/members"

    added = await client.post(base, json=_member(workingDays=["friday", "monday", "monday"]), headers=auth_headers)
    assert added.status_code == 201, added.text
    member = added.json()
    assert member["name"] == "Wanjiku"
    assert member["status"] == "pending"
    assert member["commissionPercent"] == 40
    assert member["workingDays"] == ["monday", "friday"]

    listed = (await client.get(base, headers=auth_headers)).json()["members"]
    assert [m["invitationId"] for m in listed] == [member["invitationId"]]

    updated = await client.patch(f"{base}/{member['invitationId']}", json={"commissionPercent": 45}, headers=auth_headers)
    assert updated.json()["commissionPercent"] == 45
    assert updated.json()["workingDays"] == ["monday", "friday"]  # untouched
    cleared = await client.patch(f"{base}/{member['invitationId']}", json={"workingDays": None}, headers=auth_headers)
    assert cleared.json()["workingDays"] is None

    assert (await client.delete(f"{base}/{member['invitationId']}", headers=auth_headers)).status_code == 204
    assert (await client.get(base, headers=auth_headers)).json()["members"] == []
    assert (await client.delete(f"{base}/{member['invitationId']}", headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    _member(commissionPercent=101),
    _member(workingDays=["funday"]),
    _member(role="owner"),
    {k: v for k, v in _member().items() if k != "name"},
])
async def test_add_team_member_validation(client, auth_headers, body):
    biz_id = await _business(client, auth_headers)
    resp = await client.post(f"/api/v1/businesses/{biz_id}/team/members", json=body, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_team_member_needs_contact_and_no_duplicates(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    base = f"/api/v1/businesses/{biz_id}/team/members"
    resp = await client.post(base, json=_member(phone=None), headers=auth_headers)
    assert resp.status_code == 400
    assert (await client.post(base, json=_member(), headers=auth_headers)).status_code == 201
    dup = await client.post(base, json=_member(), headers=auth_headers)
    assert dup.status_code == 409 and dup.json()["error"] == "already_invited"


@pytest.mark.asyncio
async def test_remove_accepted_member_removes_staff(client, auth_headers, db_session, test_user):
    biz_id = await _business(client, auth_headers)
    member_id = uuid.uuid4()
    inv = TeamInvitation(
        business_id=uuid.UUID(biz_id), phone="+254712000009", role="staff", status="accepted",
        invited_by=test_user.id, user_id=member_id, name="Otieno",
    )
    db_session.add(inv)
    db_session.add(Staff(business_id=uuid.UUID(biz_id), user_id=member_id, name="Otieno", role="staff"))
    await db_session.commit()

    base = f"/api/v1/businesses/{biz_id}/team/members"
    dup = await client.post(base, json=_member(phone="+254712000009"), headers=auth_headers)
    assert dup.status_code == 409 and dup.json()["error"] == "already_member"

    assert (await client.delete(f"{base}/{inv.id}", headers=auth_headers)).status_code == 204
    db_session.expire_all()
    assert (await db_session.execute(select(Staff).where(Staff.user_id == member_id))).first() is None


@pytest.mark.asyncio
async def test_team_member_other_business_is_404(client, auth_headers):
    biz_id = await _business(client, auth_headers)
    other_id = await _business(client, auth_headers)
    member = (await client.post(f"/api/v1/businesses/{other_id}/team/members", json=_member(), headers=auth_headers)).json()
    resp = await client.patch(f"/api/v1/businesses/{biz_id}/team/members/{member['invitationId']}", json={"commissionPercent": 1}, headers=auth_headers)
    assert resp.status_code == 404


# ─── Swagger ─────────────────────────────────────────────────


def test_business_routes_document_response_models():
    from main import app

    missing = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith(("/api/v1/businesses", "/api/v1/me/", "/api/v1/invitations", "/api/v1/payment-destinations")):
            continue
        for method, op in ops.items():
            for status, resp in op["responses"].items():
                if status.startswith("2") and status != "204":
                    schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
                    if "$ref" not in schema:
                        missing.append(f"{method.upper()} {path} {status}")
    assert missing == []
