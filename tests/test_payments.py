import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.modules.payments.service as payments_service
from app.modules.booking.models import Booking
from app.modules.payments.models import Payment
from app.modules.payments.mpesa import MpesaError, StkPushResult
from tests.test_create_booking import body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.fail_with = None

    async def request_payment(self, destination, *, phone, amount, reference, description):
        self.calls.append({"type": destination.type, "phone": phone, "amount": amount, "reference": reference})
        if self.fail_with:
            raise self.fail_with
        return StkPushResult(f"merchant-{len(self.calls)}", f"ws_CO_{len(self.calls)}_{uuid.uuid4().hex[:6]}",
                             "Success. Request accepted for processing")

    async def check_payment(self, checkout_request_id):
        raise NotImplementedError


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(payments_service, "get_provider", lambda: fake)
    return fake


@pytest.fixture
async def pending_booking(client, auth_headers, salon, customer):
    _, cust = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    await client.put(f"/api/v1/businesses/{salon['id']}/payment-destination",
                     json={"type": "mpesa_till", "tillNumber": "123456"}, headers=auth_headers)
    resp = await book(client, cust, body(salon))
    assert resp.json()["status"] == "pending_payment"
    return resp.json()


async def pay(client, headers, booking_id, **json):
    return await client.post(f"/api/v1/bookings/{booking_id}/payments", json=json, headers=headers)


@pytest.mark.asyncio
async def test_sends_the_prompt_routed_to_the_business(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    resp = await pay(client, cust, pending_booking["bookingId"])
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert (data["status"], data["amount"], data["currency"], data["phone"]) == ("pending", 300, "KES", "254722000111")
    assert data["customerMessage"].startswith("Success")
    assert data["expiresAt"] == pending_booking["expiresAt"]
    assert provider.calls == [{"type": "mpesa_till", "phone": "254722000111", "amount": 300, "reference": pending_booking["reference"]}]

    payment = (await db_session.execute(select(Payment))).scalar_one()
    assert (payment.destination_type, payment.party_b, payment.account_reference) == ("mpesa_till", "123456", pending_booking["reference"])
    assert payment.checkout_request_id.startswith("ws_CO_")

    seen = (await client.get(f"/api/v1/bookings/{pending_booking['bookingId']}", headers=cust)).json()
    assert seen["depositPayment"] == {"paymentId": data["paymentId"], "status": "pending", "failureReason": None}
    assert seen["status"] == "pending_payment"


@pytest.mark.asyncio
async def test_phone_override_and_validation(client, customer, pending_booking, provider):
    _, cust = customer
    bad = await pay(client, cust, pending_booking["bookingId"], phone="0201234567")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_phone"
    ok = await pay(client, cust, pending_booking["bookingId"], phone="0711 000 999")
    assert ok.json()["phone"] == "254711000999"


@pytest.mark.asyncio
async def test_one_prompt_at_a_time(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    first = await pay(client, cust, pending_booking["bookingId"])
    again = await pay(client, cust, pending_booking["bookingId"])
    assert again.status_code == 409 and again.json()["error"] == "payment_in_progress"
    assert again.json()["paymentId"] == first.json()["paymentId"]

    payment = await db_session.get(Payment, uuid.UUID(first.json()["paymentId"]))
    payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=3)  # the prompt timed out long ago
    await db_session.commit()
    assert (await pay(client, cust, pending_booking["bookingId"])).status_code == 202


@pytest.mark.asyncio
async def test_provider_error_is_recorded_and_retryable(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    provider.fail_with = MpesaError("500.001.1001", "Unable to lock subscriber")
    resp = await pay(client, cust, pending_booking["bookingId"])
    assert resp.status_code == 502 and resp.json()["error"] == "payment_provider_error"
    failed = (await db_session.execute(select(Payment))).scalar_one()
    assert (failed.status, failed.failure_reason) == ("failed", "provider_error")

    provider.fail_with = None
    assert (await pay(client, cust, pending_booking["bookingId"])).status_code == 202  # no lock after a failure


@pytest.mark.asyncio
async def test_attempt_limit(client, customer, pending_booking, provider):
    _, cust = customer
    provider.fail_with = MpesaError("x", "down")
    for _ in range(5):
        await pay(client, cust, pending_booking["bookingId"])
    resp = await pay(client, cust, pending_booking["bookingId"])
    assert resp.status_code == 429 and resp.json()["error"] == "too_many_attempts"


@pytest.mark.asyncio
async def test_expired_hold(client, customer, pending_booking, provider, db_session):
    _, cust = customer
    b = await db_session.get(Booking, uuid.UUID(pending_booking["bookingId"]))
    b.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    resp = await pay(client, cust, pending_booking["bookingId"])
    assert resp.status_code == 409 and resp.json()["error"] == "hold_expired"
    assert (await client.get(f"/api/v1/bookings/{pending_booking['bookingId']}", headers=cust)).json()["status"] == "cancelled"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_no_deposit_due(client, salon, customer, provider):
    _, cust = customer
    confirmed = (await book(client, cust, body(salon))).json()
    resp = await pay(client, cust, confirmed["bookingId"])
    assert resp.status_code == 409 and resp.json()["error"] == "no_deposit_due"


@pytest.mark.asyncio
async def test_only_the_customer_can_pay(client, auth_headers, pending_booking, provider, db_session):
    assert (await pay(client, auth_headers, pending_booking["bookingId"])).status_code == 404  # the owner
    stranger = await make_user(db_session, "payer@example.com")
    assert (await pay(client, headers_for(stranger), pending_booking["bookingId"])).status_code == 404


@pytest.mark.asyncio
async def test_business_without_payment_destination(client, auth_headers, salon, customer, provider):
    _, cust = customer
    await set_deposit(client, auth_headers, salon, {"required": True, "type": "fixed", "amount": {"amount": 300, "currency": "KES"}, "appliesTo": "all_services"})
    booking = (await book(client, cust, body(salon))).json()
    resp = await pay(client, cust, booking["bookingId"])
    assert resp.status_code == 409 and resp.json()["error"] == "business_cannot_take_payments"


@pytest.mark.asyncio
async def test_mpesa_not_configured(client, customer, pending_booking):
    _, cust = customer
    payments_service.get_provider.cache_clear()  # the real one, with the blanked test settings
    resp = await pay(client, cust, pending_booking["bookingId"])
    assert resp.status_code == 503 and resp.json()["error"] == "payments_unavailable"
