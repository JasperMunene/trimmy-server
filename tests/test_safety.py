"""Trust & safety: reports, hiding reviews, suspending businesses, payment details checks, signals."""
import uuid

import pytest
from sqlalchemy import select

from app.modules.business.models import PaymentDestination, Review
from app.modules.customers.models import BusinessCustomer
from app.modules.notifications.models import Notification
from tests.test_admin import admin, bearer, sign_in  # noqa: F401  (fixtures)
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


@pytest.fixture
async def admin_headers(client, admin):
    return bearer((await sign_in(client))["session"])


async def a_review(db_session, salon, rating=1, text="Terrible, never went"):
    review = Review(business_id=uuid.UUID(salon["id"]), staff_id=uuid.UUID(salon["a"]), author_name="Troll", rating=rating, text=text)
    db_session.add(review)
    await db_session.commit()
    return review


async def report(client, headers, **body):
    return await client.post("/api/v1/reports", json=body, headers=headers)


@pytest.mark.asyncio
async def test_who_can_report_what(client, salon, customer, auth_headers, db_session):
    _, cust_headers = customer
    assert (await report(client, cust_headers, targetType="business", targetId=salon["id"], reason="fraud")).status_code == 201
    own = await report(client, auth_headers, targetType="business", targetId=salon["id"], reason="fraud")
    assert own.status_code == 400  # the owner can't report their own business

    review = await a_review(db_session, salon)
    assert (await report(client, auth_headers, targetType="review", targetId=str(review.id), reason="fake_review")).status_code == 201

    record = BusinessCustomer(business_id=uuid.UUID(salon["id"]), name="Serial No-show", phone="+254700000111")
    db_session.add(record)
    await db_session.commit()
    assert (await report(client, auth_headers, targetType="customer", targetId=str(record.id), reason="no_show")).status_code == 201
    stranger = await make_user(db_session, "stranger.reporter@example.com")
    assert (await report(client, headers_for(stranger), targetType="customer", targetId=str(record.id), reason="no_show")).status_code == 404

    # Reporting again while it's open updates the same report.
    again = await report(client, cust_headers, targetType="business", targetId=salon["id"], reason="safety", details="Felt unsafe")
    first = (await report(client, cust_headers, targetType="business", targetId=salon["id"], reason="safety"))
    assert again.json()["reportId"] == first.json()["reportId"]


@pytest.mark.asyncio
async def test_cases_group_reports_and_hiding_a_review_drops_it_everywhere(client, salon, customer, auth_headers, admin_headers, db_session):
    _, cust_headers = customer
    good = await a_review(db_session, salon, rating=5, text="Lovely")
    bad = await a_review(db_session, salon, rating=1)
    for headers in (auth_headers, cust_headers):
        await report(client, headers, targetType="review", targetId=str(bad.id), reason="fake_review")
    cases = (await client.get("/api/v1/admin/safety/cases", headers=admin_headers)).json()["cases"]
    assert cases[0]["targetId"] == str(bad.id) and cases[0]["reportCount"] == 2

    before = (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).json()
    assert before["reviewCount"] == 2 and before["rating"] == 3.0
    hide = await client.post(f"/api/v1/admin/reviews/{bad.id}/hide", json={"reason": "Never visited"}, headers=admin_headers)
    assert hide.status_code == 200
    from app.core import cache_invalidation
    await cache_invalidation.settle()

    after = (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).json()
    assert after["reviewCount"] == 1 and after["rating"] == 5.0
    listed = (await client.get(f"/api/v1/businesses/{salon['id']}/reviews")).json()
    assert [r["reviewId"] for r in listed["reviews"]] == [str(good.id)]
    card = next(b for b in (await client.get("/api/v1/businesses/search")).json()["businesses"] if b["businessId"] == salon["id"])
    assert card["rating"] == 5.0
    staff = next(s for s in after["staff"] if s["staffId"] == salon["a"])
    assert staff["rating"] == 5.0

    done = await client.post(f"/api/v1/admin/safety/cases/review/{bad.id}/resolve", json={"outcome": "actioned", "reason": "Hidden"}, headers=admin_headers)
    assert {r["status"] for r in done.json()["reports"]} == {"actioned"}
    assert done.json()["audit"][0]["action"] == "report.action"
    assert (await client.get("/api/v1/admin/safety/cases", headers=admin_headers)).json()["cases"] == []


@pytest.mark.asyncio
async def test_suspending_hides_a_business_from_customers_but_not_its_owner(client, salon, customer, auth_headers, admin_headers, db_session, test_user):
    _, cust_headers = customer
    url = f"/api/v1/admin/businesses/{salon['id']}"
    assert (await client.post(f"{url}/suspend", json={"reason": "Fraud reports confirmed"}, headers=admin_headers)).status_code == 200
    from app.core import cache_invalidation
    await cache_invalidation.settle()

    assert (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).status_code == 404
    assert salon["id"] not in [b["businessId"] for b in (await client.get("/api/v1/businesses/search")).json()["businesses"]]
    blocked = await book(client, cust_headers, body(salon))
    assert blocked.status_code == 404
    mine = (await client.get("/api/v1/me/businesses", headers=auth_headers)).json()["businesses"][0]
    assert mine["suspendedAt"] and "paused" in mine["suspendedReason"]
    told = (await db_session.execute(select(Notification).where(Notification.user_id == test_user.id))).scalars().all()
    assert "business_suspended" in [n.type for n in told]

    await client.post(f"{url}/unsuspend", json={"reason": "Cleared"}, headers=admin_headers)
    await cache_invalidation.settle()
    assert (await client.get(f"/api/v1/businesses/{salon['id']}/profile")).status_code == 200
    assert (await book(client, cust_headers, body(salon))).status_code in (200, 201)


@pytest.mark.asyncio
async def test_checking_payment_details(client, salon, admin_headers, db_session):
    unchecked = (await client.get("/api/v1/admin/safety/destinations", headers=admin_headers)).json()
    if not any(d["businessId"] == salon["id"] for d in unchecked):
        db_session.add(PaymentDestination(business_id=uuid.UUID(salon["id"]), type="mpesa_till", till_number="5123456",
                                          verification_status="verified"))
        await db_session.commit()
        unchecked = (await client.get("/api/v1/admin/safety/destinations", headers=admin_headers)).json()
    assert salon["id"] in [d["businessId"] for d in unchecked]

    failed = await client.post(f"/api/v1/admin/businesses/{salon['id']}/destination/review",
                               json={"decision": "failed", "reason": "Till name is someone else"}, headers=admin_headers)
    assert failed.json()["verificationStatus"] == "failed"
    assert salon["id"] in [d["businessId"] for d in (await client.get("/api/v1/admin/safety/destinations", params={"filter": "failed"}, headers=admin_headers)).json()]
    ok = await client.post(f"/api/v1/admin/businesses/{salon['id']}/destination/review",
                           json={"decision": "ok", "reason": "Called the owner"}, headers=admin_headers)
    assert ok.json()["verificationStatus"] == "verified"
    assert salon["id"] not in [d["businessId"] for d in (await client.get("/api/v1/admin/safety/destinations", headers=admin_headers)).json()]


@pytest.mark.asyncio
async def test_warning_someone_and_signals(client, customer, admin_headers, db_session, salon, auth_headers):
    cust, cust_headers = customer
    warned = await client.post(f"/api/v1/admin/users/{cust.id}/warn", json={"reason": "Abusive messages", "message": "Please keep messages respectful."},
                               headers=admin_headers)
    assert warned.status_code == 200
    inbox = (await db_session.execute(select(Notification).where(Notification.user_id == cust.id, Notification.type == "trimmy_notice"))).scalars().all()
    assert inbox and inbox[0].body == "Please keep messages respectful."

    for headers in (cust_headers, headers_for(await make_user(db_session, "r2@example.com")), headers_for(await make_user(db_session, "r3@example.com"))):
        await report(client, headers, targetType="business", targetId=salon["id"], reason="fraud")
    signals = (await client.get("/api/v1/admin/safety/signals", headers=admin_headers)).json()
    assert signals["repeatReports"][0]["targetId"] == salon["id"] and signals["repeatReports"][0]["count"] == 3
    assert (await client.get("/api/v1/admin/safety/signals", headers=auth_headers)).status_code == 401
