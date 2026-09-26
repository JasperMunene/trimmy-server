import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.modules.payments.service as payments_service
import app.workers.registry as registry
from app.modules.booking.models import Booking
from app.modules.payments.models import Payment
from app.modules.payments.mpesa import MpesaError, PaymentResult
from app.workers.registry import PERIODIC, TASKS, discover_tasks
from tests.test_create_booking import body, book, customer, salon, set_deposit  # noqa: F401  (fixtures)
from tests.test_payments import pay, pending_booking, provider  # noqa: F401  (fixtures)


@pytest.fixture
async def run_job(db_session, monkeypatch):
    discover_tasks()
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))

    async def run():
        await TASKS["payments.reconcile_pending"].run({})
        db_session.expire_all()

    return run


async def aged_payment(client, cust, booking, db_session, age):
    resp = await pay(client, cust, booking["bookingId"])
    payment = await db_session.get(Payment, uuid.UUID(resp.json()["paymentId"]))
    payment.created_at = datetime.now(timezone.utc) - age
    await db_session.commit()
    return payment.id


def test_registered_every_minute():
    discover_tasks()
    assert PERIODIC["payments.reconcile_pending"].every_seconds == 60


@pytest.mark.asyncio
async def test_lost_callback_success_confirms(client, customer, pending_booking, provider, db_session, run_job):
    _, cust = customer
    payment_id = await aged_payment(client, cust, pending_booking, db_session, timedelta(seconds=90))
    queried = []

    async def check(checkout_id):
        queried.append(checkout_id)
        return PaymentResult("succeeded", None, 0, "processed", checkout_id)

    provider.check_payment = check
    await run_job()
    assert len(queried) == 1
    assert (await db_session.get(Payment, payment_id)).status == "succeeded"
    booking = await db_session.get(Booking, uuid.UUID(pending_booking["bookingId"]))
    assert (booking.status, booking.payment_status) == ("confirmed", "deposit_paid")


@pytest.mark.asyncio
async def test_young_payments_are_left_for_the_callback(client, customer, pending_booking, provider, db_session, run_job):
    _, cust = customer
    await aged_payment(client, cust, pending_booking, db_session, timedelta(seconds=10))

    async def check(checkout_id):
        raise AssertionError("should not query yet")

    provider.check_payment = check
    await run_job()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["pending", "error"])
async def test_gives_up_after_three_minutes(client, customer, pending_booking, provider, db_session, run_job, answer):
    _, cust = customer
    payment_id = await aged_payment(client, cust, pending_booking, db_session, timedelta(minutes=4))

    async def check(checkout_id):
        if answer == "error":
            raise MpesaError("500.003.02", "System busy")
        return PaymentResult("pending", None, None, "still processing", checkout_id)

    provider.check_payment = check
    await run_job()
    payment = await db_session.get(Payment, payment_id)
    assert (payment.status, payment.failure_reason) == ("timeout", "timeout")
    assert (await db_session.get(Booking, uuid.UUID(pending_booking["bookingId"]))).status == "pending_payment"


@pytest.mark.asyncio
async def test_still_pending_within_the_window_is_retried_next_time(client, customer, pending_booking, provider, db_session, run_job):
    _, cust = customer
    payment_id = await aged_payment(client, cust, pending_booking, db_session, timedelta(seconds=90))

    async def check(checkout_id):
        return PaymentResult("pending", None, None, "still processing", checkout_id)

    provider.check_payment = check
    await run_job()
    assert (await db_session.get(Payment, payment_id)).status == "pending"


@pytest.mark.asyncio
async def test_push_without_checkout_id_fails_after_the_window(client, customer, pending_booking, provider, db_session, run_job):
    _, cust = customer
    payment_id = await aged_payment(client, cust, pending_booking, db_session, timedelta(minutes=4))
    payment = await db_session.get(Payment, payment_id)
    payment.checkout_request_id = None  # as if the process died mid-push
    await db_session.commit()
    await run_job()
    payment = await db_session.get(Payment, payment_id)
    assert (payment.status, payment.failure_reason) == ("failed", "provider_error")


@pytest.mark.asyncio
async def test_unconfigured_provider_is_logged_not_raised(client, customer, pending_booking, provider, db_session, run_job, monkeypatch):
    _, cust = customer
    await aged_payment(client, cust, pending_booking, db_session, timedelta(minutes=4))
    monkeypatch.undo()  # drop the fake provider (and the session patch, so redo that)
    monkeypatch.setattr(registry, "AsyncSessionLocal", async_sessionmaker(bind=db_session.bind))
    payments_service.get_provider.cache_clear()
    await TASKS["payments.reconcile_pending"].run({})  # MpesaConfigError is handled
