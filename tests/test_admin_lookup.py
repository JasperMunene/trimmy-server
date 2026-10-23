"""Admin look-up tools: overview, users, businesses, bookings and payments."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.auth.models import User
from app.modules.payments.models import Payment
from tests.test_admin import admin, bearer, sign_in  # noqa: F401  (fixtures)
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)


@pytest.fixture
async def admin_headers(client, admin):
    return bearer((await sign_in(client))["session"])


async def booked(client, salon, customer, db_session):
    """One online booking with a successful M-Pesa deposit."""
    _, cust_headers = customer
    resp = await book(client, cust_headers, body(salon))
    assert resp.status_code in (200, 201), resp.text
    booking_id = resp.json()["bookingId"]
    now = datetime.now(timezone.utc)
    db_session.add(Payment(
        booking_id=uuid.UUID(booking_id), kind="deposit", provider="mpesa", amount=300, tip_amount=0, currency="KES",
        phone="254712345678", destination_type="mpesa_till", party_b="123456", account_reference="TRIMMY", status="succeeded",
        mpesa_receipt="SGT7XK2LQ1", checkout_request_id=f"ws_CO_{uuid.uuid4().hex[:12]}", created_at=now, settled_at=now,
    ))
    db_session.add(Payment(
        booking_id=uuid.UUID(booking_id), kind="deposit", provider="mpesa", amount=300, tip_amount=0, currency="KES",
        phone="254712345678", destination_type="mpesa_till", party_b="123456", account_reference="TRIMMY",
        status="failed", failure_reason="insufficient_funds",
        checkout_request_id=f"ws_CO_{uuid.uuid4().hex[:12]}", created_at=now,
    ))
    await db_session.commit()
    return booking_id


@pytest.mark.asyncio
async def test_everything_needs_an_admin_session(client, auth_headers):
    for path in ("/admin/overview", "/admin/users", "/admin/businesses", "/admin/bookings?q=ABC123"):
        assert (await client.get(f"/api/v1{path}", headers=auth_headers)).status_code == 401, path


@pytest.mark.asyncio
async def test_overview_counts_the_platform(client, admin_headers, salon, customer, db_session):
    await booked(client, salon, customer, db_session)
    data = (await client.get("/api/v1/admin/overview", headers=admin_headers)).json()
    assert data["businesses"]["published"] >= 1
    assert data["bookings"]["today"] >= 1 and data["bookings"]["onlineLast30d"] >= 1
    assert data["payments"]["volume30d"] >= 300
    assert data["payments"]["mpesaAttempts7d"] == 2 and data["payments"]["successRate7d"] == 50
    assert data["topFailures"][0] == {"reason": "insufficient_funds", "count": 1}
    assert len(data["series"]) == 30 and data["series"][-1]["bookings"] >= 1


@pytest.mark.asyncio
async def test_find_users_by_name_email_or_any_phone_format(client, admin_headers, db_session, test_user):
    test_user.phone = "+254712345678"
    await db_session.commit()
    for q in ("test@example", "Test", "0712 345 678", "+254712345678", "712345678"):
        found = (await client.get("/api/v1/admin/users", params={"q": q}, headers=admin_headers)).json()["users"]
        assert str(test_user.id) in [u["id"] for u in found], q
    admins = (await client.get("/api/v1/admin/users", params={"filter": "admins"}, headers=admin_headers)).json()["users"]
    assert [u["email"] for u in admins] == ["ops@trimmyadmin.com"]


@pytest.mark.asyncio
async def test_user_detail_shows_their_businesses_bookings_and_audit(client, admin_headers, salon, customer, db_session, test_user):
    booking_id = await booked(client, salon, customer, db_session)
    owner = (await client.get(f"/api/v1/admin/users/{test_user.id}", headers=admin_headers)).json()
    assert owner["workplaces"][0]["role"] == "owner" and owner["workplaces"][0]["businessId"] == salon["id"]

    cust, _ = customer
    await client.post(f"/api/v1/admin/users/{cust.id}/sign-out", json={"reason": "Lost phone"}, headers=admin_headers)
    detail = (await client.get(f"/api/v1/admin/users/{cust.id}", headers=admin_headers)).json()
    assert detail["bookings"][0]["id"] == booking_id and detail["bookingStats"]["total"] == 1
    assert detail["activeSessions"] == 0
    assert detail["audit"][0]["action"] == "user.sign_out_everywhere"
    assert (await client.get(f"/api/v1/admin/users/{uuid.uuid4()}", headers=admin_headers)).status_code == 404


@pytest.mark.asyncio
async def test_businesses_list_and_detail(client, admin_headers, salon, customer, db_session, test_user):
    await booked(client, salon, customer, db_session)
    found = (await client.get("/api/v1/admin/businesses", params={"q": "test@example.com"}, headers=admin_headers)).json()
    assert [b["id"] for b in found["businesses"]] == [salon["id"]]
    assert found["businesses"][0]["ownerEmail"] == "test@example.com"

    detail = (await client.get(f"/api/v1/admin/businesses/{salon['id']}", headers=admin_headers)).json()
    assert detail["team"] and detail["servicesCount"] >= 1
    assert detail["stats30d"]["bookings"] == 1 and detail["stats30d"]["volume"] == 300
    assert {p["status"] for p in detail["payments"]} == {"succeeded", "failed"}
    drafts = (await client.get("/api/v1/admin/businesses", params={"status": "draft"}, headers=admin_headers)).json()
    assert salon["id"] not in [b["id"] for b in drafts["businesses"]]


@pytest.mark.asyncio
async def test_find_a_booking_by_reference_receipt_or_phone(client, admin_headers, salon, customer, db_session):
    booking_id = await booked(client, salon, customer, db_session)
    detail = (await client.get(f"/api/v1/admin/bookings/{booking_id}", headers=admin_headers)).json()
    reference = detail["booking"]["reference"]

    by_ref = (await client.get("/api/v1/admin/bookings", params={"q": reference.lower()}, headers=admin_headers)).json()
    assert by_ref["matchedBy"] == "reference" and by_ref["bookings"][0]["id"] == booking_id
    by_receipt = (await client.get("/api/v1/admin/bookings", params={"q": "sgt7xk2lq1"}, headers=admin_headers)).json()
    assert by_receipt["matchedBy"] == "receipt" and by_receipt["bookings"][0]["id"] == booking_id
    by_phone = (await client.get("/api/v1/admin/bookings", params={"q": "0712345678"}, headers=admin_headers)).json()
    assert by_phone["matchedBy"] == "phone" and booking_id in [b["id"] for b in by_phone["bookings"]]

    assert detail["services"] and detail["events"][0]["toStatus"] in ("pending_payment", "confirmed")
    assert [p["mpesaReceipt"] for p in detail["payments"]].count("SGT7XK2LQ1") == 1
    assert detail["payments"][0]["destination"] == "Till 123456"


@pytest.mark.asyncio
async def test_rechecking_a_pending_payment(client, admin_headers, salon, customer, db_session, monkeypatch):
    booking_id = await booked(client, salon, customer, db_session)
    pending = Payment(booking_id=uuid.UUID(booking_id), kind="deposit", provider="mpesa", amount=300, tip_amount=0,
                      currency="KES", status="pending", checkout_request_id="ws_CO_pending1",
                      phone="254712345678", destination_type="mpesa_till", party_b="123456", account_reference="TRIMMY",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=2))
    db_session.add(pending)
    await db_session.commit()

    from app.modules.payments import mpesa
    from app.modules.payments import service as payments_service

    class Provider:
        async def check_payment(self, checkout_id):
            return mpesa.PaymentResult("failed", "cancelled", None, "Request cancelled by user", checkout_id)

    monkeypatch.setattr(payments_service, "get_provider", lambda: Provider())
    resp = await client.post(f"/api/v1/admin/payments/{pending.id}/recheck", json={"reason": "Customer says they paid"},
                             headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed" and resp.json()["failureReason"] == "cancelled"
    again = await client.post(f"/api/v1/admin/payments/{pending.id}/recheck", json={"reason": "again"}, headers=admin_headers)
    assert again.status_code == 409
    audit = (await client.get(f"/api/v1/admin/bookings/{booking_id}", headers=admin_headers)).json()["audit"]
    assert audit[0]["action"] == "payment.recheck" and audit[0]["changes"]["status"] == ["pending", "failed"]
