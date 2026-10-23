"""Search-result cards (BusinessSummaryResponse) built from a few light queries.

Search and favorites used to load every business with all its photos, services and every review
ever left, then work out ratings in Python: cost grew with businesses x reviews. Here the database
does the counting, so it's four queries whatever the review volume, and each card is a plain dict
(JSON-safe, so the search index can be cached as is).
"""
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.config import settings
from app.modules.business.models import Business, BusinessPhoto, BusinessStatus, Review, Service
from app.modules.business.schemas import BusinessSummaryResponse, MoneyResponse


async def load_cards(db: AsyncSession) -> list[dict]:
    """Every published business, as card dicts."""
    rows = (await db.execute(
        select(Business.id, Business.name, Business.categories, Business.location, Business.published_at)
        .where(Business.status == BusinessStatus.PUBLISHED)
    )).all()
    if not rows:
        return []
    ids = [r.id for r in rows]

    photos = defaultdict(list)
    for bid, url in (await db.execute(
        select(BusinessPhoto.business_id, BusinessPhoto.url)
        .where(BusinessPhoto.business_id.in_(ids))
        .order_by(BusinessPhoto.business_id, BusinessPhoto.is_cover.desc(), BusinessPhoto.created_at)
    )).all():
        photos[bid].append(url)

    services = defaultdict(list)
    for bid, name, amount, currency in (await db.execute(
        select(Service.business_id, Service.name, Service.price_amount, Service.price_currency)
        .where(Service.business_id.in_(ids))
    )).all():
        services[bid].append((name, amount, currency))

    ratings = {
        bid: (count, float(avg or 0))
        for bid, count, avg in (await db.execute(
            select(Review.business_id, func.count(Review.id), func.avg(Review.rating))
            .where(Review.business_id.in_(ids))
            .group_by(Review.business_id)
        )).all()
    }

    cards = []
    for r in rows:
        loc = r.location or {}
        offered = services.get(r.id, [])
        cheapest = min(offered, key=lambda s: s[1]) if offered else None
        count, avg = ratings.get(r.id, (0, 0.0))
        cards.append({
            "id": str(r.id),
            "name": r.name,
            "categories": r.categories or [],
            "photos": photos.get(r.id, []),
            "address": loc.get("address", ""),
            "lat": loc.get("lat", 0.0),
            "lng": loc.get("lng", 0.0),
            "publishedTs": r.published_at.timestamp() if r.published_at else 0.0,
            # For the text search: the name and every service name, lower-cased once.
            "haystack": " ".join([r.name, *(s[0] for s in offered)]).lower(),
            "prices": sorted({s[1] for s in offered}),
            "startingPrice": [cheapest[1], cheapest[2]] if cheapest else [0, "KES"],
            "rating": round(avg, 1) if count else 0.0,
            "reviewCount": count,
        })
    return cards


async def cached_cards(db: AsyncSession) -> list[dict]:
    """Every published business's card, from the shared cache when it's there."""
    return await cache.remember(cache.CARDS_KEY, settings.CACHE_SEARCH_SECONDS, lambda: load_cards(db))


def card_summary(card: dict, distance_km: float = 0.0) -> BusinessSummaryResponse:
    photos = card["photos"]
    return BusinessSummaryResponse(
        businessId=card["id"],
        name=card["name"],
        categories=card["categories"],
        thumbnailUrl=photos[0] if photos else "",
        photos=photos,
        address=card["address"],
        lat=card["lat"],
        lng=card["lng"],
        distanceKm=distance_km,
        startingPrice=MoneyResponse(amount=card["startingPrice"][0], currency=card["startingPrice"][1]),
        rating=card["rating"],
        reviewCount=card["reviewCount"],
    )
