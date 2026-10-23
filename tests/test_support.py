"""Support tickets: the person's side (Help) and the team's (admin inbox)."""
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.modules.notifications.models import Notification
from tests.test_admin import admin, bearer, sign_in  # noqa: F401  (fixtures)
from tests.test_create_booking import body, book, customer, day, salon  # noqa: F401  (fixtures)
from tests.test_read_bookings import headers_for, make_user


@pytest.fixture
async def admin_headers(client, admin):
    return bearer((await sign_in(client))["session"])


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 120)).save(buf, format="PNG")
    return buf.getvalue()


async def open_ticket(client, headers, **extra):
    resp = await client.post("/api/v1/support/tickets", headers=headers,
                             json={"category": "payment", "message": "I paid but the app says unpaid", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a_customer_asks_about_their_booking(client, salon, customer, db_session):
    _, cust_headers = customer
    booking_id = (await book(client, cust_headers, body(salon))).json()["bookingId"]
    data = await open_ticket(client, cust_headers, bookingId=booking_id)
    assert data["ticket"]["status"] == "new" and data["ticket"]["subject"] == "A payment"
    assert data["ticket"]["businessId"] == salon["id"] and data["bookingReference"]
    assert [m["kind"] for m in data["messages"]] == ["customer"]

    stranger = await make_user(db_session, "nosy@example.com")
    resp = await client.post("/api/v1/support/tickets", headers=headers_for(stranger),
                             json={"category": "booking", "message": "x", "bookingId": booking_id})
    assert resp.status_code == 404
    mine = (await client.get("/api/v1/support/tickets", headers=cust_headers)).json()["tickets"]
    assert [t["id"] for t in mine] == [data["ticket"]["id"]]
    assert (await client.get(f"/api/v1/support/tickets/{data['ticket']['id']}", headers=headers_for(stranger))).status_code == 404


@pytest.mark.asyncio
async def test_owners_can_attach_their_business_only(client, salon, auth_headers, db_session):
    ok = await open_ticket(client, auth_headers, businessId=salon["id"], category="business")
    assert ok["businessName"]
    stranger = await make_user(db_session, "other.owner@example.com")
    resp = await client.post("/api/v1/support/tickets", headers=headers_for(stranger),
                             json={"category": "business", "message": "x", "businessId": salon["id"]})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_the_team_replies_and_the_person_is_told(client, customer, admin_headers, db_session):
    cust, cust_headers = customer
    ticket = (await open_ticket(client, cust_headers))["ticket"]
    inbox = (await client.get("/api/v1/admin/support/tickets", params={"view": "unassigned"}, headers=admin_headers)).json()
    assert [t["id"] for t in inbox["tickets"]] == [ticket["id"]]
    assert (await client.get("/api/v1/admin/support/summary", headers=admin_headers)).json()["unassigned"] == 1

    note = await client.post(f"/api/v1/admin/support/tickets/{ticket['id']}/messages", headers=admin_headers,
                             data={"body": "Checked M-Pesa: payment is there", "internal": "true"})
    assert note.status_code == 200 and note.json()["ticket"]["status"] == "new"
    answered = await client.post(f"/api/v1/admin/support/tickets/{ticket['id']}/messages", headers=admin_headers,
                                 data={"body": "Sorted it: your booking now shows paid."})
    detail = answered.json()
    assert detail["ticket"]["status"] == "waiting" and detail["ticket"]["firstResponseAt"]
    assert detail["ticket"]["assigneeName"] == "Ops Admin"
    assert [m["kind"] for m in detail["messages"]] == ["customer", "note", "event", "agent"]
    assert detail["requester"]["id"] == str(cust.id)

    seen = (await client.get(f"/api/v1/support/tickets/{ticket['id']}", headers=cust_headers)).json()
    assert [m["kind"] for m in seen["messages"]] == ["customer", "agent"]  # no notes, no events
    assert seen["messages"][1]["authorName"] == "Ops · Trimmy support"
    sent = (await db_session.execute(select(Notification).where(Notification.user_id == cust.id))).scalars().all()
    assert [n.type for n in sent if n.type == "support_reply"] == ["support_reply"]


@pytest.mark.asyncio
async def test_writing_back_reopens_and_photos_attach(client, customer, admin_headers):
    _, cust_headers = customer
    ticket = (await open_ticket(client, cust_headers))["ticket"]
    await client.post(f"/api/v1/admin/support/tickets/{ticket['id']}/messages", headers=admin_headers,
                      data={"body": "All done", "status": "resolved"})
    back = await client.post(f"/api/v1/support/tickets/{ticket['id']}/messages", headers=cust_headers,
                             data={"body": "Actually here's the SMS"}, files={"photo": ("sms.png", png(), "image/png")})
    assert back.status_code == 200, back.text
    data = back.json()
    assert data["ticket"]["status"] == "open" and data["messages"][-1]["attachmentUrl"]
    bad = await client.post(f"/api/v1/support/tickets/{ticket['id']}/messages", headers=cust_headers,
                            files={"photo": ("x.txt", b"not an image", "text/plain")})
    assert bad.status_code == 400
    empty = await client.post(f"/api/v1/support/tickets/{ticket['id']}/messages", headers=cust_headers, data={"body": "  "})
    assert empty.status_code == 400

    done = await client.post(f"/api/v1/support/tickets/{ticket['id']}/resolve", headers=cust_headers)
    assert done.json()["ticket"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_assigning_statuses_and_filters(client, customer, admin_headers, admin, test_user):
    _, cust_headers = customer
    ticket = (await open_ticket(client, cust_headers))["ticket"]
    url = f"/api/v1/admin/support/tickets/{ticket['id']}"
    assert (await client.patch(url, json={"assigneeId": str(test_user.id)}, headers=admin_headers)).status_code == 400
    mine = await client.patch(url, json={"assigneeId": "me", "priority": "urgent", "category": "refund"}, headers=admin_headers)
    assert mine.json()["ticket"]["priority"] == "urgent" and mine.json()["ticket"]["assigneeId"] == str(admin.id)
    listed = (await client.get("/api/v1/admin/support/tickets", params={"view": "mine"}, headers=admin_headers)).json()
    assert len(listed["tickets"]) == 1
    await client.patch(url, json={"status": "closed"}, headers=admin_headers)
    active = (await client.get("/api/v1/admin/support/tickets", headers=admin_headers)).json()["tickets"]
    closed = (await client.get("/api/v1/admin/support/tickets", params={"status": "closed"}, headers=admin_headers)).json()["tickets"]
    assert active == [] and [t["id"] for t in closed] == [ticket["id"]]
    found = (await client.get("/api/v1/admin/support/tickets", params={"status": "all", "q": f"#{ticket['number']}"},
                              headers=admin_headers)).json()["tickets"]
    assert [t["id"] for t in found] == [ticket["id"]]


@pytest.mark.asyncio
async def test_canned_replies(client, admin_headers):
    made = await client.post("/api/v1/admin/support/macros", json={"title": "Refunds", "body": "Businesses refund within 3 days."},
                             headers=admin_headers)
    assert made.status_code == 201
    mid = made.json()["id"]
    await client.put(f"/api/v1/admin/support/macros/{mid}", json={"title": "Refunds", "body": "Within 2 days."}, headers=admin_headers)
    listed = (await client.get("/api/v1/admin/support/macros", headers=admin_headers)).json()["macros"]
    assert [m["body"] for m in listed] == ["Within 2 days."]
    await client.delete(f"/api/v1/admin/support/macros/{mid}", headers=admin_headers)
    assert (await client.get("/api/v1/admin/support/macros", headers=admin_headers)).json()["macros"] == []


@pytest.mark.asyncio
async def test_the_inbox_is_team_only(client, auth_headers):
    assert (await client.get("/api/v1/admin/support/tickets", headers=auth_headers)).status_code == 401
    assert (await client.get(f"/api/v1/support/tickets/{uuid.uuid4()}", headers=auth_headers)).status_code == 404
