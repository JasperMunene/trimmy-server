import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.modules.business.models import Review, Staff

WEEKLY_HOURS = {
    "monday": {"open": "09:00", "close": "19:00"},
    "tuesday": {"open": "09:00", "close": "19:00"},
    "wednesday": {"open": "09:00", "close": "19:00"},
    "thursday": {"open": "09:00", "close": "19:00"},
    "friday": {"open": "09:00", "close": "20:00"},
    "saturday": {"open": "09:00", "close": "20:00"},
    "sunday": None,
}


async def create_draft(
    client: AsyncClient,
    auth_headers: dict,
    *,
    name: str,
    phone: str,
    categories: list[str],
    address: str = "Nairobi",
    lat: float = -1.2921,
    lng: float = 36.7833,
    description: str | None = None,
    amenities: list[str] | None = None,
) -> str:
    payload = {
        "name": name,
        "categories": categories,
        "phone": phone,
        "location": {"address": address, "lat": lat, "lng": lng},
    }
    if description is not None:
        payload["description"] = description
    if amenities is not None:
        payload["amenities"] = amenities
    resp = await client.post("/api/v1/businesses", json=payload, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["businessId"]


async def publish_business(
    client: AsyncClient,
    auth_headers: dict,
    *,
    name: str,
    phone: str,
    categories: list[str],
    address: str = "Nairobi",
    lat: float = -1.2921,
    lng: float = 36.7833,
    category_name: str = "Haircuts",
    service_name: str = "Classic Haircut",
    price: int = 500,
    description: str | None = None,
    amenities: list[str] | None = None,
    deposit_required: bool = False,
    deposit_amount: int = 0,
    deposit_type: str = "fixed",
) -> str:
    biz_id = await create_draft(
        client,
        auth_headers,
        name=name,
        phone=phone,
        categories=categories,
        address=address,
        lat=lat,
        lng=lng,
        description=description,
        amenities=amenities,
    )

    wh = await client.put(
        f"/api/v1/businesses/{biz_id}/working-hours",
        json={"weeklyHours": WEEKLY_HOURS},
        headers=auth_headers,
    )
    assert wh.status_code == 200, wh.text

    cat = await client.post(
        f"/api/v1/businesses/{biz_id}/service-categories",
        json={"name": category_name},
        headers=auth_headers,
    )
    assert cat.status_code == 201, cat.text
    cat_id = cat.json()["categoryId"]

    svc = await client.post(
        f"/api/v1/businesses/{biz_id}/services",
        json={
            "categoryId": cat_id,
            "name": service_name,
            "durationMinutes": 45,
            "price": {"amount": price, "currency": "KES"},
        },
        headers=auth_headers,
    )
    assert svc.status_code == 201, svc.text

    pol = await client.put(
        f"/api/v1/businesses/{biz_id}/policies",
        json={
            "deposit": {
                "required": deposit_required,
                "type": deposit_type,
                "amount": {"amount": deposit_amount, "currency": "KES"},
                "appliesTo": "all_services",
            },
            "cancellation": {"freeCancellationHours": 24, "lateFeePercent": 50},
            "noShow": {"feePercent": 100},
        },
        headers=auth_headers,
    )
    assert pol.status_code == 200, pol.text

    pay = await client.put(
        f"/api/v1/businesses/{biz_id}/payment-destination",
        json={"type": "mpesa_till", "tillNumber": "123456"},
        headers=auth_headers,
    )
    assert pay.status_code == 200, pay.text

    pub = await client.patch(
        f"/api/v1/businesses/{biz_id}/publish",
        json={"visible": True},
        headers=auth_headers,
    )
    assert pub.status_code == 200, pub.text
    return biz_id


# ─── Search ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_200_without_auth(client: AsyncClient):
    resp = await client.get("/api/v1/businesses/search")
    assert resp.status_code == 200
    assert resp.json() == {"businesses": []}


@pytest.mark.asyncio
async def test_search_returns_only_published(client: AsyncClient, auth_headers: dict):
    draft_id = await create_draft(
        client,
        auth_headers,
        name="Draft Salon",
        phone="+254720000001",
        categories=["hair_salon"],
    )
    pub_id = await publish_business(
        client,
        auth_headers,
        name="Live Salon",
        phone="+254720000002",
        categories=["hair_salon"],
    )

    resp = await client.get("/api/v1/businesses/search")
    assert resp.status_code == 200
    ids = [b["businessId"] for b in resp.json()["businesses"]]
    assert pub_id in ids
    assert draft_id not in ids


@pytest.mark.asyncio
async def test_search_filters_by_category(client: AsyncClient, auth_headers: dict):
    await publish_business(
        client,
        auth_headers,
        name="Barber One",
        phone="+254720000003",
        categories=["barbershop"],
    )
    await publish_business(
        client,
        auth_headers,
        name="Spa One",
        phone="+254720000004",
        categories=["spa"],
    )

    resp = await client.get("/api/v1/businesses/search", params={"category": "spa"})
    assert resp.status_code == 200
    businesses = resp.json()["businesses"]
    assert len(businesses) == 1
    assert businesses[0]["name"] == "Spa One"
    assert businesses[0]["categories"] == ["spa"]


@pytest.mark.asyncio
async def test_search_filters_by_price_max(client: AsyncClient, auth_headers: dict):
    cheap_id = await publish_business(
        client,
        auth_headers,
        name="Budget Cuts",
        phone="+254720000005",
        categories=["barbershop"],
        service_name="Buzz Cut",
        price=500,
    )
    await publish_business(
        client,
        auth_headers,
        name="Luxury Cuts",
        phone="+254720000006",
        categories=["barbershop"],
        service_name="Executive Cut",
        price=5000,
    )

    resp = await client.get("/api/v1/businesses/search", params={"priceMax": 1000})
    assert resp.status_code == 200
    businesses = resp.json()["businesses"]
    assert len(businesses) == 1
    assert businesses[0]["businessId"] == cheap_id
    assert businesses[0]["startingPrice"]["amount"] == 500


@pytest.mark.asyncio
async def test_search_filters_by_rating_min(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    rated_id = await publish_business(
        client,
        auth_headers,
        name="Rated Salon",
        phone="+254720000007",
        categories=["hair_salon"],
    )
    unrated_id = await publish_business(
        client,
        auth_headers,
        name="Unrated Salon",
        phone="+254720000008",
        categories=["hair_salon"],
    )

    db_session.add_all(
        [
            Review(
                business_id=uuid.UUID(rated_id),
                author_name="Amina K.",
                rating=5,
                text="Great",
            ),
            Review(
                business_id=uuid.UUID(rated_id),
                author_name="Faith N.",
                rating=4,
                text="Fine",
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/v1/businesses/search", params={"ratingMin": 4})
    assert resp.status_code == 200
    businesses = resp.json()["businesses"]
    ids = [b["businessId"] for b in businesses]
    assert rated_id in ids
    assert unrated_id not in ids

    rated = next(b for b in businesses if b["businessId"] == rated_id)
    assert rated["rating"] == 4.5
    assert rated["reviewCount"] == 2

    resp = await client.get("/api/v1/businesses/search", params={"ratingMin": 5})
    ids = [b["businessId"] for b in resp.json()["businesses"]]
    assert rated_id not in ids


@pytest.mark.asyncio
async def test_search_filters_by_query(client: AsyncClient, auth_headers: dict):
    await publish_business(
        client,
        auth_headers,
        name="Glow Beauty Lounge",
        phone="+254720000009",
        categories=["hair_salon"],
        service_name="Classic Haircut",
    )
    await publish_business(
        client,
        auth_headers,
        name="Urban Fade Barbershop",
        phone="+254720000010",
        categories=["barbershop"],
        service_name="Skin Fade",
    )

    resp = await client.get("/api/v1/businesses/search", params={"query": "haircut"})
    assert resp.status_code == 200
    businesses = resp.json()["businesses"]
    assert len(businesses) == 1
    assert businesses[0]["name"] == "Glow Beauty Lounge"


@pytest.mark.asyncio
async def test_search_sorts_by_distance_and_applies_radius(
    client: AsyncClient, auth_headers: dict
):
    near_id = await publish_business(
        client,
        auth_headers,
        name="Near Salon",
        phone="+254720000011",
        categories=["hair_salon"],
        address="Origin",
        lat=0.0,
        lng=0.0,
    )
    far_id = await publish_business(
        client,
        auth_headers,
        name="Far Salon",
        phone="+254720000012",
        categories=["hair_salon"],
        address="Far Away",
        lat=0.0,
        lng=1.0,
    )

    resp = await client.get(
        "/api/v1/businesses/search", params={"lat": 0.0, "lng": 0.0}
    )
    assert resp.status_code == 200
    businesses = resp.json()["businesses"]
    assert [b["businessId"] for b in businesses] == [near_id, far_id]
    assert businesses[0]["distanceKm"] == 0.0
    assert businesses[1]["distanceKm"] > 0

    resp = await client.get(
        "/api/v1/businesses/search",
        params={"lat": 0.0, "lng": 0.0, "radiusKm": 50},
    )
    ids = [b["businessId"] for b in resp.json()["businesses"]]
    assert ids == [near_id]


@pytest.mark.asyncio
async def test_search_includes_starting_price_and_thumbnail(
    client: AsyncClient, auth_headers: dict
):
    biz_id = await publish_business(
        client,
        auth_headers,
        name="Photo Salon",
        phone="+254720000013",
        categories=["hair_salon"],
        price=800,
    )

    photo = await client.post(
        f"/api/v1/businesses/{biz_id}/photos",
        files={"photo": ("cover.jpg", b"fake_image_data", "image/jpeg")},
        data={"isCover": "true"},
        headers=auth_headers,
    )
    assert photo.status_code == 201
    photo_url = photo.json()["url"]

    resp = await client.get("/api/v1/businesses/search")
    assert resp.status_code == 200
    summary = resp.json()["businesses"][0]
    assert summary["startingPrice"] == {"amount": 800, "currency": "KES"}
    assert summary["thumbnailUrl"] == photo_url
    assert summary["photos"] == [photo_url]
    assert summary["address"] == "Nairobi"
    assert "reviewCount" in summary
    assert "rating" in summary


# ─── Profile ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_happy_path(client: AsyncClient, auth_headers: dict):
    biz_id = await publish_business(
        client,
        auth_headers,
        name="Glow Beauty Lounge",
        phone="+254720000014",
        categories=["hair_salon", "nail_salon"],
        address="Kilimani, Nairobi",
        lat=-1.2921,
        lng=36.7833,
        description="A cosy beauty lounge in the heart of Kilimani.",
        amenities=["Free WiFi", "Parking available"],
        deposit_required=True,
        deposit_amount=200,
        deposit_type="fixed",
    )

    resp = await client.get(f"/api/v1/businesses/{biz_id}/profile")
    assert resp.status_code == 200
    data = resp.json()

    assert data["businessId"] == biz_id
    assert data["name"] == "Glow Beauty Lounge"
    assert data["description"] == "A cosy beauty lounge in the heart of Kilimani."
    assert data["categories"] == ["hair_salon", "nail_salon"]
    assert data["photos"] == []
    assert data["rating"] == 0.0
    assert data["reviewCount"] == 0
    assert data["address"] == "Kilimani, Nairobi"
    assert data["lat"] == -1.2921
    assert data["lng"] == 36.7833
    assert data["depositSummary"] == "KES 200 deposit required"
    assert (
        data["cancellationSummary"]
        == "Free cancellation up to 24h before, 50% fee after"
    )
    assert data["amenities"] == ["Free WiFi", "Parking available"]

    wh = data["workingHours"]
    assert set(wh.keys()) == {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
    assert wh["monday"] == {"open": "09:00", "close": "19:00"}
    assert wh["sunday"] is None

    policies = data["policies"]
    assert policies["deposit"]["required"] is True
    assert policies["deposit"]["type"] == "fixed"
    assert policies["deposit"]["amount"] == {"amount": 200, "currency": "KES"}
    assert policies["deposit"]["appliesTo"] == "all_services"
    assert policies["cancellation"] == {"freeCancellationHours": 24, "lateFeePercent": 50}
    assert policies["noShow"] == {"feePercent": 100}

    assert len(data["services"]) == 1
    service = data["services"][0]
    assert service["categoryName"] == "Haircuts"
    assert service["name"] == "Classic Haircut"
    assert service["durationMinutes"] == 45
    assert service["price"] == {"amount": 500, "currency": "KES"}
    assert "serviceId" in service

    assert len(data["staff"]) == 1
    staff = data["staff"][0]
    assert staff["role"] == "owner"
    assert staff["name"] == "Test User"
    assert staff["rating"] == 0.0
    assert staff["specialties"] == []
    assert staff["portfolio"] == []
    assert "staffId" in staff

    assert data["reviews"] == []


@pytest.mark.asyncio
async def test_profile_no_deposit_summary(client: AsyncClient, auth_headers: dict):
    biz_id = await publish_business(
        client,
        auth_headers,
        name="No Deposit Salon",
        phone="+254720000015",
        categories=["spa"],
    )

    resp = await client.get(f"/api/v1/businesses/{biz_id}/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["depositSummary"] == "No deposit required"
    assert data["policies"]["deposit"]["required"] is False


@pytest.mark.asyncio
async def test_profile_not_found_draft(client: AsyncClient, auth_headers: dict):
    biz_id = await create_draft(
        client,
        auth_headers,
        name="Hidden Draft",
        phone="+254720000016",
        categories=["hair_salon"],
    )

    resp = await client.get(f"/api/v1/businesses/{biz_id}/profile")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_profile_not_found_unknown_id(client: AsyncClient):
    resp = await client.get(f"/api/v1/businesses/{uuid.uuid4()}/profile")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert resp.json()["message"] == "No published business found for this id"


@pytest.mark.asyncio
async def test_profile_not_found_invalid_uuid(client: AsyncClient):
    resp = await client.get("/api/v1/businesses/not-a-uuid/profile")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ─── Staff hooks ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_staff_created_on_solo_publish(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    biz_id = await publish_business(
        client,
        auth_headers,
        name="Solo Publish",
        phone="+254720000017",
        categories=["barbershop"],
    )

    staff_rows = (
        (
            await db_session.execute(
                select(Staff).where(Staff.business_id == uuid.UUID(biz_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(staff_rows) == 1
    assert staff_rows[0].role == "owner"
    assert staff_rows[0].user_id == test_user.id
    assert staff_rows[0].name == "Test User"


@pytest.mark.asyncio
async def test_staff_created_on_invitation_accept(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    biz_id = await create_draft(
        client,
        auth_headers,
        name="Team Business",
        phone="+254720000018",
        categories=["hair_salon"],
    )
    await client.patch(
        f"/api/v1/businesses/{biz_id}/team-mode",
        json={"teamMode": "team"},
        headers=auth_headers,
    )

    invite = await client.post(
        f"/api/v1/businesses/{biz_id}/team/invitations",
        json={"phone": "+1234567890", "role": "front_desk"},
        headers=auth_headers,
    )
    assert invite.status_code == 201, invite.text
    inv_id = invite.json()["invitationId"]

    resp = await client.post(
        f"/api/v1/invitations/{inv_id}/respond",
        json={"action": "accept"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    staff_rows = (
        (
            await db_session.execute(
                select(Staff).where(
                    Staff.business_id == uuid.UUID(biz_id),
                    Staff.user_id == test_user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(staff_rows) == 1
    assert staff_rows[0].role == "front_desk"
    assert staff_rows[0].name == "Test User"


# ─── Draft fields ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_draft_returns_description_and_amenities(
    client: AsyncClient, auth_headers: dict
):
    biz_id = await create_draft(
        client,
        auth_headers,
        name="Described Draft",
        phone="+254720000019",
        categories=["nail_salon"],
        description="Nail art specialists.",
        amenities=["Free WiFi", "Card payment accepted"],
    )

    resp = await client.get(f"/api/v1/businesses/{biz_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Nail art specialists."
    assert data["amenities"] == ["Free WiFi", "Card payment accepted"]
