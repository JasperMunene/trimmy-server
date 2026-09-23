import uuid
import pytest
from httpx import AsyncClient

from app.modules.auth.models import User


@pytest.mark.asyncio
async def test_create_business(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Glow Beauty Lounge",
            "description": "A cosy beauty lounge in Kilimani.",
            "categories": ["hair_salon", "nail_salon"],
            "phone": "+254712345678",
            "location": {
                "address": "Kilimani, Nairobi",
                "lat": -1.2921,
                "lng": 36.7833,
                "building": "Yaya Centre",
                "floor": "2nd Floor",
                "unit": "Shop 14",
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Glow Beauty Lounge"
    assert data["description"] == "A cosy beauty lounge in Kilimani."
    assert data["categories"] == ["hair_salon", "nail_salon"]
    assert data["status"] == "draft"
    assert data["onboardingStep"] == "business_basics"
    assert "businessId" in data


@pytest.mark.asyncio
async def test_create_business_duplicate_phone(client: AsyncClient, auth_headers: dict):
    payload = {
        "name": "Business A",
        "categories": ["hair_salon"],
        "phone": "+254700000001",
        "location": {"address": "Nairobi", "lat": -1.0, "lng": 36.0},
    }
    resp1 = await client.post("/api/v1/businesses", json=payload, headers=auth_headers)
    assert resp1.status_code == 201

    payload["name"] = "Business B"
    resp2 = await client.post("/api/v1/businesses", json=payload, headers=auth_headers)
    assert resp2.status_code == 409
    assert resp2.json()["error"] == "phone_already_registered"


@pytest.mark.asyncio
async def test_create_business_validation_error(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/businesses",
        json={"name": "", "categories": [], "phone": "+254712345678", "location": {"address": "Nairobi", "lat": 0, "lng": 0}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_business(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Test Business",
            "categories": ["spa"],
            "phone": "+254722222222",
            "location": {"address": "Westlands", "lat": -1.2676, "lng": 36.8025},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["businessId"] == biz_id
    assert data["name"] == "Test Business"
    assert data["status"] == "draft"
    assert "photos" in data
    assert "serviceCategories" in data


@pytest.mark.asyncio
async def test_get_business_not_found(client: AsyncClient, auth_headers: dict):
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/v1/businesses/{fake_id}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_photo(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Photo Test",
            "categories": ["beauty"],
            "phone": "+254733333333",
            "location": {"address": "CBD", "lat": -1.2864, "lng": 36.8172},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("test.jpg", b"fake_image_data", "image/jpeg")},
        data={"isCover": "true"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["isCover"] is True
    assert data["businessId"] == biz_id
    assert "photoId" in data


@pytest.mark.asyncio
async def test_set_working_hours(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Hours Test",
            "categories": ["barbershop"],
            "phone": "+254744444444",
            "location": {"address": "Kasarani", "lat": -1.2266, "lng": 36.8874},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/working-hours",
        json={
            "weeklyHours": {
                "monday": {"open": "09:00", "close": "19:00"},
                "tuesday": {"open": "09:00", "close": "19:00"},
                "wednesday": {"open": "09:00", "close": "19:00"},
                "thursday": {"open": "09:00", "close": "19:00"},
                "friday": {"open": "09:00", "close": "20:00"},
                "saturday": {"open": "09:00", "close": "20:00"},
                "sunday": None,
            }
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["weeklyHours"]["monday"]["open"] == "09:00"
    assert data["weeklyHours"]["sunday"] is None
    assert data["onboardingStep"] == "working_hours"


@pytest.mark.asyncio
async def test_set_working_hours_invalid_time(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Bad Hours",
            "categories": ["spa"],
            "phone": "+254755555555",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/working-hours",
        json={"weeklyHours": {"monday": {"open": "19:00", "close": "09:00"}}},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_create_service_category(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Category Test",
            "categories": ["hair_salon"],
            "phone": "+254766666666",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Haircuts"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Haircuts"
    assert data["businessId"] == biz_id
    assert "categoryId" in data


@pytest.mark.asyncio
async def test_list_service_categories(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "List Cat Test",
            "categories": ["nail_salon"],
            "phone": "+254777777777",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Nail Art"},
        headers=auth_headers,
    )

    resp = await client.get(f"/api/v1/businesses/{biz_id}/service-categories", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["categories"]) == 1
    assert data["categories"][0]["name"] == "Nail Art"


@pytest.mark.asyncio
async def test_create_service(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Service Test",
            "categories": ["hair_salon"],
            "phone": "+254788888888",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    cat_resp = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Haircuts"},
        headers=auth_headers,
    )
    cat_id = cat_resp.json()["categoryId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat_id,
            "name": "Classic Haircut",
            "durationMinutes": 45,
            "price": {"amount": 800, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Classic Haircut"
    assert data["durationMinutes"] == 45
    assert data["price"]["amount"] == 800


@pytest.mark.asyncio
async def test_create_service_invalid_category(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Bad Cat",
            "categories": ["spa"],
            "phone": "+254799999999",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    fake_cat = str(uuid.uuid4())
    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": fake_cat,
            "name": "Bad Service",
            "durationMinutes": 30,
            "price": {"amount": 500, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_create_service_zero_price(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Zero Price",
            "categories": ["beauty"],
            "phone": "+254701111111",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    cat_resp = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Facials"},
        headers=auth_headers,
    )
    cat_id = cat_resp.json()["categoryId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat_id,
            "name": "Free Facial",
            "durationMinutes": 30,
            "price": {"amount": 0, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_set_policies(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Policy Test",
            "categories": ["spa"],
            "phone": "+254702222222",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json={
            "deposit": {
                "required": False,
                "type": "fixed",
                "amount": {"amount": 0, "currency": "KES"},
                "appliesTo": "all_services",
            },
            "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50},
            "noShow": {"feePercent": 100},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deposit"]["required"] is False
    assert data["cancellation"]["freeCancellationHours"] == 24
    assert data["noShow"]["feePercent"] == 100
    assert data["onboardingStep"] == "policies"


@pytest.mark.asyncio
async def test_set_payment_destination(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Payment Test",
            "categories": ["hair_salon"],
            "phone": "+254703333333",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={
            "type": "bank_account",
            "bankName": "Equity Bank Kenya",
            "bankShortcode": "247247",
            "bankAccountNumber": "1234567890",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "bank_account"
    assert data["bankName"] == "Equity Bank Kenya"
    assert data["verificationStatus"] == "verified"
    assert data["onboardingStep"] == "payment_destination"


@pytest.mark.asyncio
async def test_set_payment_destination_missing_fields(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Missing Fields",
            "categories": ["spa"],
            "phone": "+254704444444",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={"type": "bank_account"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_set_team_mode(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Team Test",
            "categories": ["barbershop"],
            "phone": "+254705555555",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/team-mode",
        json={"teamMode": "solo"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["teamMode"] == "solo"
    assert data["onboardingStep"] == "team_mode"


@pytest.mark.asyncio
async def test_set_team_mode_invalid_value(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Bad Team",
            "categories": ["spa"],
            "phone": "+254706666666",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/team-mode",
        json={"teamMode": "invalid"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invite_team_member(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Invite Test",
            "categories": ["hair_salon"],
            "phone": "+254707777777",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/team/invitations",
        json={"email": "jane@example.com", "role": "front_desk"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "jane@example.com"
    assert data["role"] == "front_desk"
    assert data["status"] == "pending"
    assert "invitationId" in data


@pytest.mark.asyncio
async def test_invite_team_member_no_contact(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "No Contact",
            "categories": ["spa"],
            "phone": "+254708888888",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/team/invitations",
        json={"role": "staff"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_invite_team_member_duplicate(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Dup Invite",
            "categories": ["nail_salon"],
            "phone": "+254709999999",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    await client.post(
        f"/api/v1/businesses/{biz_id}/team/invitations",
        json={"email": "dup@example.com", "role": "staff"},
        headers=auth_headers,
    )

    resp = await client.post(
        f"/api/v1/businesses/{biz_id}/team/invitations",
        json={"email": "dup@example.com", "role": "staff"},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "already_invited"


@pytest.mark.asyncio
async def test_publish_business(client: AsyncClient, auth_headers: dict, db_session, test_user):
    from sqlalchemy import select
    from app.modules.business.models import Staff

    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Publish Test",
            "categories": ["hair_salon"],
            "phone": "+254710101010",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    await client.put(
        f"/api/v1/businesses/{biz_id}/working-hours",
        json={"weeklyHours": {"monday": {"open": "09:00", "close": "18:00"}}},
        headers=auth_headers,
    )

    cat_resp = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Hair"},
        headers=auth_headers,
    )
    cat_id = cat_resp.json()["categoryId"]

    await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={"categoryId": cat_id, "name": "Cut", "durationMinutes": 30, "price": {"amount": 500, "currency": "KES"}},
        headers=auth_headers,
    )

    await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json={
            "deposit": {"required": False, "type": "fixed", "amount": {"amount": 0, "currency": "KES"}, "appliesTo": "all_services"},
            "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50},
            "noShow": {"feePercent": 100},
        },
        headers=auth_headers,
    )

    await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={"type": "mpesa_till", "tillNumber": "123456"},
        headers=auth_headers,
    )

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/publish",
        json={"visible": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["visible"] is True
    assert data["publishedAt"] is not None

    staff_rows = (
        (await db_session.execute(select(Staff).where(Staff.business_id == uuid.UUID(biz_id))))
        .scalars()
        .all()
    )
    assert len(staff_rows) == 1
    assert staff_rows[0].role == "owner"
    assert staff_rows[0].user_id == test_user.id


@pytest.mark.asyncio
async def test_publish_business_incomplete(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": "Incomplete",
            "categories": ["spa"],
            "phone": "+254711111111",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    biz_id = create_resp.json()["businessId"]

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/publish",
        json={"visible": True},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "incomplete_setup"


@pytest.mark.asyncio
async def test_unauthorized_access(client: AsyncClient):
    resp = await client.get("/api/v1/businesses/some-id")
    assert resp.status_code == 403 or resp.status_code == 401


@pytest.mark.asyncio
async def test_business_access_other_users_business(client: AsyncClient, auth_headers: dict, test_user, db_session):
    from app.modules.business.models import Business, BusinessStatus
    from app.modules.auth.models import AuthType

    other_user = User(
        id=uuid.uuid4(),
        email="other@example.com",
        first_name="Other",
        last_name="User",
        auth_type=AuthType.EMAIL,
        verified=True,
    )
    db_session.add(other_user)
    await db_session.commit()

    biz = Business(
        owner_id=other_user.id,
        name="Other Business",
        phone="+254799999999",
        categories=["spa"],
        location={"address": "Nairobi", "lat": 0, "lng": 0},
        status=BusinessStatus.DRAFT,
    )
    db_session.add(biz)
    await db_session.commit()
    await db_session.refresh(biz)

    resp = await client.get(f"/api/v1/businesses/{biz.id}", headers=auth_headers)
    assert resp.status_code == 404


# ─── Team Invitation Endpoints ───────────────────────────────


@pytest.mark.asyncio
async def test_list_my_invitations_empty(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/me/invitations", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["invitations"] == []


@pytest.mark.asyncio
async def test_list_my_invitations(client: AsyncClient, auth_headers: dict, db_session):
    from datetime import datetime, timezone
    from app.modules.business.models import Business, TeamInvitation, InvitationStatus, BusinessStatus
    user = (await db_session.execute(
        __import__('sqlalchemy').select(User).where(User.email == "test@example.com")
    )).scalar_one()
    biz = Business(
        owner_id=user.id,
        name="Test Salon",
        phone="+254712345678",
        categories=["hair_salon"],
        location={"address": "Nairobi", "lat": 0, "lng": 0},
        status=BusinessStatus.DRAFT,
    )
    db_session.add(biz)
    await db_session.flush()
    inv = TeamInvitation(
        business_id=biz.id,
        phone="+1234567890",
        role="front_desk",
        status=InvitationStatus.PENDING,
        invited_by=user.id,
    )
    db_session.add(inv)
    await db_session.commit()
    await db_session.refresh(inv)
    resp = await client.get("/api/v1/me/invitations", headers=auth_headers)
    assert resp.status_code == 200
    invitations = resp.json()["invitations"]
    assert len(invitations) == 1
    assert invitations[0]["businessName"] == "Test Salon"
    assert invitations[0]["role"] == "front_desk"
    assert invitations[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_respond_to_invitation_accept(client: AsyncClient, auth_headers: dict, db_session):
    from app.modules.business.models import Business, TeamInvitation, InvitationStatus, BusinessStatus
    user = (await db_session.execute(
        __import__('sqlalchemy').select(User).where(User.email == "test@example.com")
    )).scalar_one()
    biz = Business(
        owner_id=user.id,
        name="Test Salon",
        phone="+254712345678",
        categories=["hair_salon"],
        location={"address": "Nairobi", "lat": 0, "lng": 0},
        status=BusinessStatus.DRAFT,
    )
    db_session.add(biz)
    await db_session.flush()
    inv = TeamInvitation(
        business_id=biz.id,
        phone="+1234567890",
        role="staff",
        status=InvitationStatus.PENDING,
        invited_by=user.id,
    )
    db_session.add(inv)
    await db_session.commit()
    await db_session.refresh(inv)
    resp = await client.post(
        f"/api/v1/invitations/{inv.id}/respond",
        json={"action": "accept"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_respond_to_invitation_decline(client: AsyncClient, auth_headers: dict, db_session):
    from app.modules.business.models import Business, TeamInvitation, InvitationStatus, BusinessStatus
    user = (await db_session.execute(
        __import__('sqlalchemy').select(User).where(User.email == "test@example.com")
    )).scalar_one()
    biz = Business(
        owner_id=user.id,
        name="Test Salon",
        phone="+254712345678",
        categories=["hair_salon"],
        location={"address": "Nairobi", "lat": 0, "lng": 0},
        status=BusinessStatus.DRAFT,
    )
    db_session.add(biz)
    await db_session.flush()
    inv = TeamInvitation(
        business_id=biz.id,
        phone="+1234567890",
        role="staff",
        status=InvitationStatus.PENDING,
        invited_by=user.id,
    )
    db_session.add(inv)
    await db_session.commit()
    await db_session.refresh(inv)
    resp = await client.post(
        f"/api/v1/invitations/{inv.id}/respond",
        json={"action": "decline"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "declined"


@pytest.mark.asyncio
async def test_respond_to_invitation_not_found(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/invitations/00000000-0000-0000-0000-000000000000/respond",
        json={"action": "accept"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_respond_to_invitation_already_responded(client: AsyncClient, auth_headers: dict, db_session):
    from app.modules.business.models import Business, TeamInvitation, InvitationStatus, BusinessStatus
    user = (await db_session.execute(
        __import__('sqlalchemy').select(User).where(User.email == "test@example.com")
    )).scalar_one()
    biz = Business(
        owner_id=user.id,
        name="Test Salon",
        phone="+254712345678",
        categories=["hair_salon"],
        location={"address": "Nairobi", "lat": 0, "lng": 0},
        status=BusinessStatus.DRAFT,
    )
    db_session.add(biz)
    await db_session.flush()
    inv = TeamInvitation(
        business_id=biz.id,
        phone="+1234567890",
        role="staff",
        status=InvitationStatus.ACCEPTED,
        invited_by=user.id,
    )
    db_session.add(inv)
    await db_session.commit()
    await db_session.refresh(inv)
    resp = await client.post(
        f"/api/v1/invitations/{inv.id}/respond",
        json={"action": "accept"},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "already_responded"


# ─── Business management (post-publish) ──────────────────────


async def _create_business(client: AsyncClient, auth_headers: dict, *, name: str, phone: str) -> str:
    resp = await client.post(
        "/api/v1/businesses",
        json={
            "name": name,
            "description": "Original description",
            "categories": ["hair_salon"],
            "phone": phone,
            "location": {"address": "Nairobi", "lat": -1.29, "lng": 36.78},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["businessId"]


@pytest.mark.asyncio
async def test_update_business_info(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Edit Me", phone="+254730000001")

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}",
        json={
            "name": "Edited Salon",
            "description": "Updated description",
            "categories": ["spa", "beauty"],
            "phone": "+254730000001",
            "location": {"address": "Westlands, Nairobi", "lat": -1.26, "lng": 36.80},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Edited Salon"
    assert data["description"] == "Updated description"
    assert data["categories"] == ["spa", "beauty"]
    assert data["phone"] == "+254730000001"
    assert data["location"]["address"] == "Westlands, Nairobi"


@pytest.mark.asyncio
async def test_update_business_duplicate_phone(client: AsyncClient, auth_headers: dict):
    await _create_business(client, auth_headers, name="Phone Holder", phone="+254730000002")
    biz_id = await _create_business(client, auth_headers, name="Phone Changer", phone="+254730000003")

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}",
        json={
            "name": "Phone Changer",
            "description": "x",
            "categories": ["spa"],
            "phone": "+254730000002",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "phone_already_registered"


@pytest.mark.asyncio
async def test_update_business_not_found(client: AsyncClient, auth_headers: dict):
    resp = await client.patch(
        f"/api/v1/businesses/{uuid.uuid4()}",
        json={
            "name": "X",
            "description": "x",
            "categories": ["spa"],
            "phone": "+254730000004",
            "location": {"address": "Nairobi", "lat": 0, "lng": 0},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_delete_business_photo_promotes_next_cover(
    client: AsyncClient, auth_headers: dict
):
    biz_id = await _create_business(client, auth_headers, name="Photo Del", phone="+254730000005")

    cover = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("cover.jpg", b"cover_bytes", "image/jpeg")},
        data={"isCover": "true"},
        headers=auth_headers,
    )
    assert cover.status_code == 201
    gallery = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("g.jpg", b"gallery_bytes", "image/jpeg")},
        data={"isCover": "false"},
        headers=auth_headers,
    )
    assert gallery.status_code == 201

    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/photos/{cover.json()['photoId']}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    draft = await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)
    photos = draft.json()["photos"]
    assert len(photos) == 1
    assert photos[0]["photoId"] == gallery.json()["photoId"]
    assert photos[0]["isCover"] is True


@pytest.mark.asyncio
async def test_delete_business_photo_not_found(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Photo 404", phone="+254730000006")
    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/photos/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_update_service(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Svc Edit", phone="+254730000007")
    cat = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Haircuts"},
        headers=auth_headers,
    )
    svc = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat.json()["categoryId"],
            "name": "Classic Cut",
            "durationMinutes": 30,
            "price": {"amount": 500, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert svc.status_code == 201
    service_id = svc.json()["serviceId"]

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/services/{service_id}",
        json={
            "name": "Executive Cut",
            "durationMinutes": 45,
            "price": {"amount": 900, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Executive Cut"
    assert data["durationMinutes"] == 45
    assert data["price"]["amount"] == 900
    assert data["categoryId"] == cat.json()["categoryId"]


@pytest.mark.asyncio
async def test_update_service_zero_price(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Svc Zero", phone="+254730000008")
    cat = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Cuts"},
        headers=auth_headers,
    )
    svc = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat.json()["categoryId"],
            "name": "Cut",
            "durationMinutes": 30,
            "price": {"amount": 500, "currency": "KES"},
        },
        headers=auth_headers,
    )
    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/services/{svc.json()['serviceId']}",
        json={"name": "Free", "durationMinutes": 30, "price": {"amount": 0, "currency": "KES"}},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_update_service_not_found(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Svc 404", phone="+254730000009")
    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/services/{uuid.uuid4()}",
        json={"name": "X", "durationMinutes": 30, "price": {"amount": 100, "currency": "KES"}},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_delete_service(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Svc Del", phone="+254730000010")
    cat = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Cuts"},
        headers=auth_headers,
    )
    svc = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat.json()["categoryId"],
            "name": "Cut",
            "durationMinutes": 30,
            "price": {"amount": 500, "currency": "KES"},
        },
        headers=auth_headers,
    )

    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/services/{svc.json()['serviceId']}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    cats_resp = await client.get(
        f"/api/v1/businesses/{biz_id}/service-categories", headers=auth_headers
    )
    assert cats_resp.json()["categories"][0]["services"] == []


@pytest.mark.asyncio
async def test_delete_service_not_found(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Svc Del 404", phone="+254730000011")
    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/services/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_service_category_cascades_services(
    client: AsyncClient, auth_headers: dict
):
    biz_id = await _create_business(client, auth_headers, name="Cat Del", phone="+254730000012")
    cat = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": "Nails"},
        headers=auth_headers,
    )
    cat_id = cat.json()["categoryId"]
    await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat_id,
            "name": "Manicure",
            "durationMinutes": 40,
            "price": {"amount": 800, "currency": "KES"},
        },
        headers=auth_headers,
    )

    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/service-categories/{cat_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    draft = await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)
    assert draft.json()["serviceCategories"] == []


@pytest.mark.asyncio
async def test_delete_service_category_not_found(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Cat 404", phone="+254730000013")
    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/service-categories/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_invalid_path_uuids_return_404(client: AsyncClient, auth_headers: dict):
    biz_id = await _create_business(client, auth_headers, name="Bad Path Ids", phone="+254730000014")

    bad = "not-a-uuid"
    body = {"name": "x", "durationMinutes": 30, "price": {"amount": 100, "currency": "KES"}}

    resp = await client.patch(
        f"/api/v1/businesses/{bad}", json={**body, "categories": ["hair_salon"], "phone": "+254730000014",
        "location": {"address": "Nairobi", "lat": 0, "lng": 0}},
        headers=auth_headers,
    )
    assert resp.status_code == 404

    resp = await client.delete(f"/api/v1/businesses/{bad}/photos/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404

    resp = await client.delete(f"/api/v1/businesses/{biz_id}/photos/not-a-uuid", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"

    resp = await client.patch(
        f"/api/v1/businesses/{biz_id}/services/not-a-uuid", json=body, headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"

    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/services/not-a-uuid", headers=auth_headers
    )
    assert resp.status_code == 404

    resp = await client.delete(
        f"/api/v1/businesses/{biz_id}/service-categories/not-a-uuid", headers=auth_headers
    )
    assert resp.status_code == 404
