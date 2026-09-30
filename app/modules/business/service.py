import logging
import re
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Optional
import uuid as uuid_pkg
from fastapi import HTTPException, UploadFile
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.phone_utils import InvalidPhoneNumberError, normalise_phone
from app.core.storage import IMAGE_TYPES, DirectUploadUnsupported, get_storage, sniff_image
from app.modules.auth.models import User
from app.modules.business.models import (
    Business,
    BusinessPhoto,
    BusinessPolicies,
    BusinessStatus,
    InvitationStatus,
    OnboardingStep,
    PaymentDestination,
    PaymentDestinationType,
    Review,
    Service,
    ServiceCategory,
    Staff,
    StaffService,
    TeamInvitation,
    TeamMode,
    VerificationStatus,
    WorkingHours,
)
from app.modules.business.schemas import (
    AddTeamMemberRequest,
    BookingSettingsResponse,
    BusinessProfileResponse,
    BusinessSummaryResponse,
    CancellationPolicyResponse,
    CompletePhotoUploadRequest,
    CreateBusinessRequest,
    CreateBusinessResponse,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    DepositRuleResponse,
    GetBusinessResponse,
    DayHoursResponse,
    InviteTeamMemberRequest,
    ListServiceCategoriesResponse,
    ListTeamMembersResponse,
    LocationResponse,
    MoneyResponse,
    NoShowPolicyResponse,
    PaymentDestinationResponse,
    PhotoResponse,
    PhotoUploadUrlRequest,
    PhotoUploadUrlResponse,
    PoliciesResponse,
    ProfilePoliciesResponse,
    ProfileServiceResponse,
    PublishBusinessRequest,
    PublishBusinessResponse,
    ReviewResponse,
    SearchResponse,
    ServiceCategoryResponse,
    ServiceCategoryWithServicesResponse,
    ServiceResponse,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetStaffServicesRequest,
    SetTeamModeRequest,
    StaffMemberResponse,
    StaffServicesResponse,
    TeamMemberResponse,
    TeamModeResponse,
    UpdateBookingSettingsRequest,
    UpdateBusinessResponse,
    UpdateServiceRequest,
    UpdateTeamMemberRequest,
    WeeklyHoursRequest,
    WeeklyHoursResponse,
    WorkingHoursResponse,
)
from app.modules.business.tasks import delete_stored_object

logger = logging.getLogger(__name__)


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometres."""
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def derive_deposit_summary(policies: Optional[BusinessPolicies]) -> str:
    if not policies or not policies.deposit_required:
        return "No deposit required"
    if policies.deposit_type == "percent":
        return f"{policies.deposit_percent or 0}% deposit required"
    amount = policies.deposit_amount or 0
    currency = policies.deposit_currency or "KES"
    return f"{currency} {amount} deposit required"


def derive_cancellation_summary(policies: Optional[BusinessPolicies]) -> str:
    if not policies:
        return "Free cancellation up to 24h before, 50% fee after"
    return (
        f"Free cancellation up to {policies.cancellation_free_hours}h before, "
        f"{policies.cancellation_late_fee_percent}% fee after"
    )


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": "validation_error", "message": message})


# ─── Response builders (shared so every endpoint returns the same shapes) ──


def weekly_hours_response(raw: Optional[dict]) -> WeeklyHoursResponse:
    # Tolerates rows saved before hours were validated: anything malformed reads as closed.
    raw = raw or {}
    days = {}
    for day in _WEEKDAYS:
        hours = raw.get(day)
        valid = isinstance(hours, dict) and isinstance(hours.get("open"), str) and isinstance(hours.get("close"), str)
        days[day] = DayHoursResponse(open=hours["open"], close=hours["close"]) if valid else None
    return WeeklyHoursResponse(**days)


def service_response(s: Service) -> ServiceResponse:
    return ServiceResponse(
        serviceId=s.id,
        businessId=s.business_id,
        categoryId=s.category_id,
        name=s.name,
        durationMinutes=s.duration_minutes,
        price=MoneyResponse(amount=s.price_amount, currency=s.price_currency),
        createdAt=s.created_at,
    )


def category_response(cat: ServiceCategory) -> ServiceCategoryWithServicesResponse:
    return ServiceCategoryWithServicesResponse(
        categoryId=cat.id,
        businessId=cat.business_id,
        name=cat.name,
        createdAt=cat.created_at,
        services=[service_response(s) for s in sorted(cat.services, key=lambda s: s.created_at)],
    )


def deposit_response(p: Optional[BusinessPolicies]) -> DepositRuleResponse:
    if not p:
        return DepositRuleResponse(required=False, amount=MoneyResponse(amount=0, currency="KES"))
    is_percent = p.deposit_type == "percent"
    return DepositRuleResponse(
        required=p.deposit_required,
        type=p.deposit_type,
        amount=None if is_percent else MoneyResponse(amount=p.deposit_amount or 0, currency=p.deposit_currency or "KES"),
        percent=p.deposit_percent if is_percent else None,
        appliesTo=p.deposit_applies_to,
        serviceIds=p.deposit_service_ids or [],
    )


def policies_response(business: Business, p: BusinessPolicies) -> PoliciesResponse:
    return PoliciesResponse(
        businessId=business.id,
        deposit=deposit_response(p),
        cancellation=CancellationPolicyResponse(
            freeCancellationHours=p.cancellation_free_hours,
            lateFeePercent=p.cancellation_late_fee_percent,
        ),
        noShow=NoShowPolicyResponse(feePercent=p.no_show_fee_percent),
        onboardingStep=business.onboarding_step,
    )


def payment_destination_response(business: Business, pd: PaymentDestination) -> PaymentDestinationResponse:
    return PaymentDestinationResponse(
        businessId=pd.business_id,
        type=pd.type,
        tillNumber=pd.till_number,
        paybillNumber=pd.paybill_number,
        paybillAccountNumber=pd.paybill_account_number,
        bankName=pd.bank_name,
        bankShortcode=pd.bank_shortcode,
        bankAccountNumber=pd.bank_account_number,
        verificationStatus=pd.verification_status,
        onboardingStep=business.onboarding_step,
    )


def photo_response(p: BusinessPhoto) -> PhotoResponse:
    return PhotoResponse(photoId=p.id, businessId=p.business_id, url=p.url, isCover=p.is_cover, createdAt=p.created_at)


def booking_settings_response(b: Business) -> BookingSettingsResponse:
    return BookingSettingsResponse(
        businessId=b.id,
        timezone=b.timezone,
        slotIntervalMinutes=b.slot_interval_minutes,
        bufferMinutes=b.buffer_minutes,
        minLeadMinutes=b.min_lead_minutes,
        maxDaysAhead=b.max_days_ahead,
    )


def invite_email(raw: Optional[str]) -> Optional[str]:
    """Emails are matched case-insensitively."""
    return (raw or "").strip().lower() or None


def invite_phone(raw: Optional[str]) -> Optional[str]:
    """E.164 when it's a real number, so "0722 000 111" matches "+254722000111"; otherwise as typed."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return normalise_phone(raw)
    except InvalidPhoneNumberError:
        return raw


def invitations_for(user: User):
    """Pending-invitation filter for this user: their (verified) email or their phone."""
    matches = []
    if invite_email(user.email):
        matches.append(func.lower(TeamInvitation.email) == invite_email(user.email))
    if invite_phone(user.phone):
        matches.append(TeamInvitation.phone.in_({user.phone, invite_phone(user.phone)}))
    return or_(*matches) if matches else None


def business_summary(biz: Business, distance_km: float = 0.0) -> BusinessSummaryResponse:
    """The search-result card. Needs photos, service_categories.services and reviews loaded."""
    services = [s for cat in (biz.service_categories or []) for s in (cat.services or [])]
    reviews = list(biz.reviews or [])
    loc = biz.location or {}
    photo_urls = [p.url for p in sorted(biz.photos or [], key=lambda p: (not p.is_cover, p.created_at))]
    if services:
        cheapest = min(services, key=lambda s: s.price_amount)
        starting_price = MoneyResponse(amount=cheapest.price_amount, currency=cheapest.price_currency)
    else:
        starting_price = MoneyResponse(amount=0, currency="KES")
    return BusinessSummaryResponse(
        businessId=biz.id,
        name=biz.name,
        categories=biz.categories or [],
        thumbnailUrl=photo_urls[0] if photo_urls else "",
        photos=photo_urls,
        address=loc.get("address", ""),
        lat=loc.get("lat", 0.0),
        lng=loc.get("lng", 0.0),
        distanceKm=distance_km,
        startingPrice=starting_price,
        rating=round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else 0.0,
        reviewCount=len(reviews),
    )


def team_member_response(inv: TeamInvitation) -> TeamMemberResponse:
    return TeamMemberResponse(
        invitationId=inv.id,
        businessId=inv.business_id,
        staffId=inv.staff_id,
        name=inv.name,
        email=inv.email,
        phone=inv.phone,
        role=inv.role,
        status=inv.status,
        commissionPercent=inv.commission_percent if inv.commission_percent is not None else 40,
        workingDays=inv.working_days,
        sentAt=inv.sent_at,
    )


class BusinessService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _parse_path_id(raw: str, entity: str) -> uuid_pkg.UUID:
        try:
            return uuid_pkg.UUID(str(raw))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"{entity} does not exist for this business"},
            )

    async def _get_owned_business(self, business_id: str, user_id: str) -> Business:
        try:
            bid = uuid_pkg.UUID(str(business_id))
            uid = uuid_pkg.UUID(str(user_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Business not found"},
            )
        result = await self.db.execute(
            select(Business).where(
                Business.id == bid,
                Business.owner_id == uid,
            )
        )
        business = result.scalar_one_or_none()
        if not business:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Business not found"},
            )
        return business

    async def _get_business_with_relations(self, business_id: str, user_id: str) -> Business:
        try:
            bid = uuid_pkg.UUID(str(business_id))
            uid = uuid_pkg.UUID(str(user_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Business not found"},
            )
        result = await self.db.execute(
            select(Business)
            .options(
                selectinload(Business.photos),
                selectinload(Business.working_hours),
                selectinload(Business.service_categories).selectinload(ServiceCategory.services),
                selectinload(Business.policies),
                selectinload(Business.payment_destination),
            )
            .where(
                Business.id == bid,
                Business.owner_id == uid,
            )
        )
        business = result.scalar_one_or_none()
        if not business:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "Business not found"},
            )
        return business

    # ─── create-business ─────────────────────────────────────

    async def create_business(self, user: User, data: CreateBusinessRequest) -> CreateBusinessResponse:
        phone_check = await self.db.execute(
            select(Business).where(Business.phone == data.phone)
        )
        if phone_check.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail={"error": "phone_already_registered", "message": "A business already exists for this phone number"},
            )

        business = Business(
            owner_id=user.id,
            name=data.name,
            phone=data.phone,
            categories=data.categories,
            location=data.location.model_dump(),
            description=data.description,
            amenities=data.amenities,
            status=BusinessStatus.DRAFT,
            onboarding_step=OnboardingStep.BUSINESS_BASICS,
        )
        self.db.add(business)
        await self.db.commit()
        await self.db.refresh(business)

        return CreateBusinessResponse(
            businessId=business.id,
            name=business.name,
            description=business.description,
            categories=business.categories,
            phone=business.phone,
            location=LocationResponse(**business.location),
            amenities=business.amenities or [],
            status=business.status,
            onboardingStep=business.onboarding_step,
            createdAt=business.created_at,
        )

    # ─── update-business-info ────────────────────────────────

    async def update_business(self, business_id: str, user_id: str, data: CreateBusinessRequest) -> UpdateBusinessResponse:
        business = await self._get_owned_business(business_id, user_id)

        if data.phone != business.phone:
            phone_check = await self.db.execute(
                select(Business).where(Business.phone == data.phone)
            )
            if phone_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=409,
                    detail={"error": "phone_already_registered", "message": "A business already exists for this phone number"},
                )

        business.name = data.name
        business.description = data.description
        business.categories = data.categories
        business.phone = data.phone
        business.location = data.location.model_dump()
        if data.amenities is not None:  # left out = unchanged (older app versions don't send it)
            business.amenities = data.amenities
        await self.db.commit()
        await self.db.refresh(business)

        return UpdateBusinessResponse(
            name=business.name,
            description=business.description,
            categories=business.categories,
            phone=business.phone,
            location=LocationResponse(**business.location),
            amenities=business.amenities or [],
        )

    # ─── get-business-draft ──────────────────────────────────

    async def get_business(self, business_id: str, user_id: str) -> GetBusinessResponse:
        business = await self._get_business_with_relations(business_id, user_id)

        working_hours = None
        if business.working_hours:
            working_hours = WorkingHoursResponse(
                businessId=business.working_hours.business_id,
                weeklyHours=weekly_hours_response(business.working_hours.weekly_hours),
                onboardingStep=business.onboarding_step,
            )

        return GetBusinessResponse(
            businessId=business.id,
            name=business.name,
            categories=business.categories,
            phone=business.phone,
            description=business.description,
            amenities=business.amenities,
            location=LocationResponse(**business.location) if business.location else None,
            photos=[photo_response(p) for p in sorted(business.photos, key=lambda p: p.created_at)],
            workingHours=working_hours,
            serviceCategories=[
                category_response(c) for c in sorted(business.service_categories, key=lambda c: c.created_at)
            ],
            policies=policies_response(business, business.policies) if business.policies else None,
            paymentDestination=(
                payment_destination_response(business, business.payment_destination)
                if business.payment_destination
                else None
            ),
            teamMode=business.team_mode,
            bookingSettings=booking_settings_response(business),
            status=business.status,
            onboardingStep=business.onboarding_step,
        )

    # ─── upload-business-photo ───────────────────────────────
    # Preferred: presigned direct upload (create_photo_upload_url → client PUTs
    # to R2 → complete_photo_upload). Multipart upload_photo also works and is
    # the only option with the local storage backend.

    @staticmethod
    def _photo_key(business_id: uuid_pkg.UUID, photo_id: uuid_pkg.UUID, content_type: str) -> str:
        return f"businesses/{business_id}/photos/{photo_id}.{IMAGE_TYPES[content_type]}"

    @staticmethod
    def _too_large() -> HTTPException:
        mb = settings.MAX_PHOTO_BYTES // (1024 * 1024)
        return HTTPException(
            status_code=413,
            detail={"error": "file_too_large", "message": f"Photo exceeds the maximum upload size of {mb} MB"},
        )

    async def create_photo_upload_url(
        self, business_id: str, user_id: str, data: PhotoUploadUrlRequest
    ) -> PhotoUploadUrlResponse:
        business = await self._get_owned_business(business_id, user_id)
        if data.size_bytes > settings.MAX_PHOTO_BYTES:
            raise self._too_large()

        key = self._photo_key(business.id, uuid_pkg.uuid4(), data.content_type)
        try:
            url = await get_storage().presign_upload(
                key, data.content_type, data.size_bytes, settings.UPLOAD_URL_EXPIRE_SECONDS
            )
        except DirectUploadUnsupported:
            raise HTTPException(
                status_code=501,
                detail={
                    "error": "direct_upload_unavailable",
                    "message": "Direct uploads aren't available on this server; use multipart POST /photos",
                },
            )
        return PhotoUploadUrlResponse(
            uploadUrl=url,
            headers={"Content-Type": data.content_type},
            key=key,
            expiresIn=settings.UPLOAD_URL_EXPIRE_SECONDS,
        )

    async def complete_photo_upload(
        self, business_id: str, user_id: str, data: CompletePhotoUploadRequest
    ) -> PhotoResponse:
        business = await self._get_owned_business(business_id, user_id)

        match = re.fullmatch(rf"businesses/{business.id}/photos/([0-9a-f-]{{36}})\.(jpg|png)", data.key)
        if not match:
            raise _validation_error("key was not issued for this business")
        photo_id = uuid_pkg.UUID(match.group(1))

        existing = await self.db.get(BusinessPhoto, photo_id)
        if existing:
            # Retried completion: same answer as the first time.
            return photo_response(existing)

        storage = get_storage()
        stored = await storage.stat(data.key)
        if stored is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "upload_not_found", "message": "No uploaded file found for this key; upload it first"},
            )
        if stored.size > settings.MAX_PHOTO_BYTES:
            await storage.delete(data.key)
            raise self._too_large()
        actual_type = sniff_image(await storage.read_head(data.key, 16))
        if actual_type is None or IMAGE_TYPES[actual_type] != match.group(2):
            await storage.delete(data.key)
            raise _validation_error("photo must be a JPEG or PNG image")

        return await self._add_photo(business, photo_id, data.key, storage.public_url(data.key), data.is_cover)

    async def upload_photo(self, business_id: str, user_id: str, photo: UploadFile, is_cover: bool) -> PhotoResponse:
        business = await self._get_owned_business(business_id, user_id)

        data = await photo.read(settings.MAX_PHOTO_BYTES + 1)
        if not data:
            raise _validation_error("photo file is required")
        if len(data) > settings.MAX_PHOTO_BYTES:
            raise self._too_large()
        content_type = sniff_image(data)
        if content_type is None:
            raise _validation_error("photo must be a JPEG or PNG image")

        photo_id = uuid_pkg.uuid4()
        key = self._photo_key(business.id, photo_id, content_type)
        url = await get_storage().save(key, data, content_type)
        try:
            return await self._add_photo(business, photo_id, key, url, is_cover)
        except Exception:
            await delete_stored_object.enqueue(key=key)
            raise

    async def _add_photo(
        self, business: Business, photo_id: uuid_pkg.UUID, key: str, url: str, is_cover: bool
    ) -> PhotoResponse:
        current = (
            await self.db.execute(select(BusinessPhoto).where(BusinessPhoto.business_id == business.id))
        ).scalars().all()
        # Exactly one cover: the first photo always is, and a new cover replaces the old one.
        is_cover = is_cover or not any(p.is_cover for p in current)
        if is_cover:
            for p in current:
                p.is_cover = False

        photo = BusinessPhoto(id=photo_id, business_id=business.id, url=url, storage_key=key, is_cover=is_cover)
        self.db.add(photo)
        business.onboarding_step = OnboardingStep.PHOTOS
        await self.db.commit()
        await self.db.refresh(photo)
        return photo_response(photo)

    async def delete_photo(self, business_id: str, user_id: str, photo_id: str) -> None:
        business = await self._get_owned_business(business_id, user_id)
        photo = await self.db.get(BusinessPhoto, self._parse_path_id(photo_id, "photoId"))
        if not photo or photo.business_id != business.id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "photoId does not exist for this business"},
            )

        was_cover, key = photo.is_cover, photo.storage_key
        await self.db.delete(photo)

        if was_cover:
            remaining = (
                await self.db.execute(
                    select(BusinessPhoto)
                    .where(BusinessPhoto.business_id == business.id, BusinessPhoto.id != photo.id)
                    .order_by(BusinessPhoto.created_at)
                )
            ).scalars().all()
            if remaining:
                remaining[0].is_cover = True

        await self.db.commit()
        if key:
            await delete_stored_object.enqueue(key=key)

    # ─── set-working-hours ───────────────────────────────────

    async def set_working_hours(self, business_id: str, user_id: str, weekly_hours: WeeklyHoursRequest) -> WorkingHoursResponse:
        business = await self._get_owned_business(business_id, user_id)

        for day in _WEEKDAYS:
            hours = getattr(weekly_hours, day)
            # HH:MM strings compare correctly once the format is validated.
            if hours is not None and hours.open >= hours.close:
                raise _validation_error(f"{day}: close time must be after open time")

        stored = weekly_hours.model_dump()  # all seven days, closed ones as null
        existing = await self.db.execute(
            select(WorkingHours).where(WorkingHours.business_id == business.id)
        )
        wh = existing.scalar_one_or_none()
        if wh:
            wh.weekly_hours = stored
        else:
            wh = WorkingHours(business_id=business.id, weekly_hours=stored)
            self.db.add(wh)

        business.onboarding_step = OnboardingStep.WORKING_HOURS
        await self.db.commit()

        return WorkingHoursResponse(
            businessId=business.id,
            weeklyHours=weekly_hours_response(stored),
            onboardingStep=business.onboarding_step,
        )

    # ─── create-service-category ─────────────────────────────

    async def create_service_category(self, business_id: str, user_id: str, data: CreateServiceCategoryRequest) -> ServiceCategoryResponse:
        business = await self._get_owned_business(business_id, user_id)

        category = ServiceCategory(
            business_id=business.id,
            name=data.name,
        )
        self.db.add(category)
        business.onboarding_step = OnboardingStep.SERVICES
        await self.db.commit()
        await self.db.refresh(category)

        return ServiceCategoryResponse(
            categoryId=category.id,
            businessId=category.business_id,
            name=category.name,
            createdAt=category.created_at,
        )

    # ─── list-service-categories ─────────────────────────────

    async def list_service_categories(self, business_id: str, user_id: str) -> ListServiceCategoriesResponse:
        business = await self._get_owned_business(business_id, user_id)

        result = await self.db.execute(
            select(ServiceCategory)
            .options(selectinload(ServiceCategory.services))
            .where(ServiceCategory.business_id == business.id)
            .order_by(ServiceCategory.created_at)
        )
        return ListServiceCategoriesResponse(categories=[category_response(c) for c in result.scalars()])

    # ─── create-service ──────────────────────────────────────

    async def create_service(self, business_id: str, user_id: str, data: CreateServiceRequest) -> ServiceResponse:
        business = await self._get_owned_business(business_id, user_id)

        cat_result = await self.db.execute(
            select(ServiceCategory).where(
                ServiceCategory.id == data.category_id,
                ServiceCategory.business_id == business.id,
            )
        )
        if not cat_result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "categoryId does not exist for this business"},
            )

        if data.price.amount <= 0:
            raise HTTPException(
                status_code=400,
                detail={"error": "validation_error", "message": "price.amount must be greater than 0"},
            )

        service = Service(
            business_id=business.id,
            category_id=data.category_id,
            name=data.name,
            duration_minutes=data.duration_minutes,
            price_amount=data.price.amount,
            price_currency=data.price.currency,
        )
        self.db.add(service)
        await self.db.commit()
        await self.db.refresh(service)

        return service_response(service)

    # ─── update-service ──────────────────────────────────────

    async def update_service(
        self, business_id: str, user_id: str, service_id: str, data: UpdateServiceRequest
    ) -> ServiceResponse:
        business = await self._get_owned_business(business_id, user_id)

        service = await self.db.get(Service, self._parse_path_id(service_id, "serviceId"))
        if not service or service.business_id != business.id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "serviceId does not exist for this business"},
            )
        if data.price.amount <= 0:
            raise HTTPException(
                status_code=400,
                detail={"error": "validation_error", "message": "price.amount must be greater than 0"},
            )

        service.name = data.name
        service.duration_minutes = data.duration_minutes
        service.price_amount = data.price.amount
        service.price_currency = data.price.currency
        await self.db.commit()
        await self.db.refresh(service)

        return service_response(service)

    async def delete_service(self, business_id: str, user_id: str, service_id: str) -> None:
        business = await self._get_owned_business(business_id, user_id)
        service = await self.db.get(Service, self._parse_path_id(service_id, "serviceId"))
        if not service or service.business_id != business.id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "serviceId does not exist for this business"},
            )
        await self.db.delete(service)
        await self._forget_deposit_services(business.id, {service.id})
        await self.db.execute(delete(StaffService).where(StaffService.service_id == service.id))
        await self.db.commit()

    async def delete_service_category(self, business_id: str, user_id: str, category_id: str) -> None:
        business = await self._get_owned_business(business_id, user_id)
        category = await self.db.get(ServiceCategory, self._parse_path_id(category_id, "categoryId"))
        if not category or category.business_id != business.id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "categoryId does not exist for this business"},
            )
        service_ids = set(
            (await self.db.execute(select(Service.id).where(Service.category_id == category.id))).scalars()
        )
        await self.db.delete(category)
        await self._forget_deposit_services(business.id, service_ids)
        if service_ids:
            await self.db.execute(delete(StaffService).where(StaffService.service_id.in_(service_ids)))
        await self.db.commit()

    async def _forget_deposit_services(self, business_id: uuid_pkg.UUID, service_ids: set) -> None:
        """Drop deleted services from a selected_services deposit rule."""
        policies = (
            await self.db.execute(select(BusinessPolicies).where(BusinessPolicies.business_id == business_id))
        ).scalar_one_or_none()
        if policies and policies.deposit_service_ids:
            gone = {str(i) for i in service_ids}
            policies.deposit_service_ids = [i for i in policies.deposit_service_ids if i not in gone]

    # ─── set-policies ────────────────────────────────────────

    async def set_policies(self, business_id: str, user_id: str, data: SetPoliciesRequest) -> PoliciesResponse:
        business = await self._get_owned_business(business_id, user_id)
        deposit = data.deposit

        # Only the fields for the chosen type/scope are kept, so stale values can't linger.
        amount = deposit.amount if deposit.type == "fixed" else None
        percent = deposit.percent if deposit.type == "percent" else None
        service_ids = None
        if deposit.applies_to == "selected_services":
            service_ids = list(dict.fromkeys(str(i) for i in (deposit.service_ids or [])))

        if deposit.required:
            if deposit.type == "fixed" and (amount is None or amount.amount <= 0):
                raise _validation_error("deposit.amount.amount must be greater than 0 for a fixed deposit")
            if deposit.type == "percent" and not percent:
                raise _validation_error("deposit.percent must be between 1 and 100 for a percent deposit")
            if deposit.applies_to == "selected_services" and not service_ids:
                raise _validation_error("deposit.serviceIds is required when appliesTo is selected_services")

        if service_ids:
            owned = {
                str(i)
                for i in (
                    await self.db.execute(
                        select(Service.id).where(
                            Service.business_id == business.id,
                            Service.id.in_([uuid_pkg.UUID(i) for i in service_ids]),
                        )
                    )
                ).scalars()
            }
            unknown = [i for i in service_ids if i not in owned]
            if unknown:
                raise _validation_error(f"deposit.serviceIds contains services not in this business: {', '.join(unknown)}")

        existing = await self.db.execute(
            select(BusinessPolicies).where(BusinessPolicies.business_id == business.id)
        )
        policies = existing.scalar_one_or_none()
        if not policies:
            policies = BusinessPolicies(business_id=business.id)
            self.db.add(policies)

        policies.deposit_required = deposit.required
        policies.deposit_type = deposit.type
        policies.deposit_amount = amount.amount if amount else None
        policies.deposit_currency = amount.currency if amount else None
        policies.deposit_percent = percent
        policies.deposit_applies_to = deposit.applies_to
        policies.deposit_service_ids = service_ids
        policies.cancellation_free_hours = data.cancellation.free_cancellation_hours
        policies.cancellation_late_fee_percent = data.cancellation.late_fee_percent
        policies.no_show_fee_percent = data.no_show.fee_percent

        business.onboarding_step = OnboardingStep.POLICIES
        await self.db.commit()

        return policies_response(business, policies)

    # ─── set-payment-destination ─────────────────────────────

    async def set_payment_destination(self, business_id: str, user_id: str, data: SetPaymentDestinationRequest) -> PaymentDestinationResponse:
        business = await self._get_owned_business(business_id, user_id)
        fields = self._validate_payment_destination(data)

        existing = await self.db.execute(
            select(PaymentDestination).where(PaymentDestination.business_id == business.id)
        )
        pd = existing.scalar_one_or_none()
        if not pd:
            pd = PaymentDestination(business_id=business.id)
            self.db.add(pd)

        pd.type = data.type
        # Clear fields from a previously saved type.
        for name in ("till_number", "paybill_number", "paybill_account_number", "bank_name", "bank_shortcode", "bank_account_number"):
            setattr(pd, name, fields.get(name))
        # "verified" currently means the details passed format validation. A real
        # check (e.g. an M-Pesa account-name lookup) would start at PENDING and
        # be completed by a worker task.
        pd.verification_status = VerificationStatus.VERIFIED

        business.onboarding_step = OnboardingStep.PAYMENT_DESTINATION
        await self.db.commit()

        return payment_destination_response(business, pd)

    @staticmethod
    def _validate_payment_destination(data: SetPaymentDestinationRequest) -> dict:
        """Check the fields for data.type and return them normalised (spaces removed)."""
        def digits(value: Optional[str]) -> str:
            return re.sub(r"[\s-]", "", value or "")

        if data.type == PaymentDestinationType.MPESA_TILL:
            till = digits(data.till_number)
            if not till:
                raise _validation_error("tillNumber is required for type mpesa_till")
            if not re.fullmatch(r"\d{5,7}", till):
                raise _validation_error("tillNumber must be 5-7 digits")
            return {"till_number": till}

        if data.type == PaymentDestinationType.MPESA_PAYBILL:
            paybill, account = digits(data.paybill_number), (data.paybill_account_number or "").strip()
            if not paybill or not account:
                raise _validation_error("paybillNumber and paybillAccountNumber are required for type mpesa_paybill")
            if not re.fullmatch(r"\d{5,6}", paybill):
                raise _validation_error("paybillNumber must be 5-6 digits")
            if len(account) > 50:
                raise _validation_error("paybillAccountNumber must be at most 50 characters")
            return {"paybill_number": paybill, "paybill_account_number": account}

        name, shortcode, account = (data.bank_name or "").strip(), digits(data.bank_shortcode), digits(data.bank_account_number)
        if not name or not shortcode or not account:
            raise _validation_error("bankName, bankShortcode, and bankAccountNumber are required for type bank_account")
        if not re.fullmatch(r"\d{5,6}", shortcode):
            raise _validation_error("bankShortcode must be 5-6 digits")
        if not re.fullmatch(r"[A-Za-z0-9]{5,34}", account):
            raise _validation_error("bankAccountNumber must be 5-34 letters or digits")
        return {"bank_name": name, "bank_shortcode": shortcode, "bank_account_number": account}

    # ─── set-team-mode ───────────────────────────────────────

    async def set_team_mode(self, business_id: str, user_id: str, data: SetTeamModeRequest) -> TeamModeResponse:
        business = await self._get_owned_business(business_id, user_id)
        business.team_mode = data.team_mode
        # Only moves the wizard forward: switching later (e.g. a published solo business adding its
        # first team member) mustn't send the owner back into setup.
        steps = list(OnboardingStep)
        if business.status != BusinessStatus.PUBLISHED and (
            business.onboarding_step is None or steps.index(OnboardingStep(business.onboarding_step)) < steps.index(OnboardingStep.TEAM_MODE)
        ):
            business.onboarding_step = OnboardingStep.TEAM_MODE
        # A published business going solo needs the owner bookable. Going to team keeps the
        # owner's Staff row as it is (still bookable, bookings stay theirs; they can drop out of
        # the schedule by setting their services to none).
        if business.status == BusinessStatus.PUBLISHED and data.team_mode == TeamMode.SOLO:
            await self._ensure_owner_staff(business)
        await self.db.commit()

        return TeamModeResponse(
            businessId=business.id,
            teamMode=business.team_mode,
            onboardingStep=business.onboarding_step,
        )

    # ─── invite-team-member ──────────────────────────────────

    async def invite_team_member(self, business_id: str, user_id: str, data: InviteTeamMemberRequest) -> TeamMemberResponse:
        business = await self._get_owned_business(business_id, user_id)
        invitation = await self._create_invitation(business, user_id, data)
        return team_member_response(invitation)

    async def _create_invitation(
        self, business: Business, user_id: str, data: InviteTeamMemberRequest, **member_fields
    ) -> TeamInvitation:
        email, phone = invite_email(data.email), invite_phone(data.phone)
        if not email and not phone:
            raise _validation_error("email or phone is required")

        for field, value in (("email", email), ("phone", phone)):
            if not value:
                continue
            column = func.lower(TeamInvitation.email) if field == "email" else TeamInvitation.phone
            clash = (
                await self.db.execute(
                    select(TeamInvitation.status).where(
                        TeamInvitation.business_id == business.id,
                        column == value,
                        TeamInvitation.status.in_([InvitationStatus.PENDING, InvitationStatus.ACCEPTED]),
                    )
                )
            ).scalars().first()
            if clash == InvitationStatus.PENDING:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already_invited", "message": f"This {field} already has a pending invitation"},
                )
            if clash == InvitationStatus.ACCEPTED:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already_member", "message": f"This {field} already belongs to a team member"},
                )

        invitation = TeamInvitation(
            business_id=business.id,
            email=email,
            phone=phone,
            role=data.role,
            status=InvitationStatus.PENDING,
            invited_by=user_id,
            **member_fields,
        )
        self.db.add(invitation)
        await self.db.commit()
        await self.db.refresh(invitation)
        return invitation

    # ─── booking settings ────────────────────────────────────

    async def update_booking_settings(
        self, business_id: str, user_id: str, data: UpdateBookingSettingsRequest
    ) -> BookingSettingsResponse:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        business = await self._get_owned_business(business_id, user_id)
        fields = data.model_fields_set
        for name in fields:
            if getattr(data, name) is None:
                raise _validation_error(f"{name} cannot be null")

        if "timezone" in fields:
            try:
                ZoneInfo(data.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise _validation_error(f"Unknown timezone {data.timezone!r}; use an IANA name like Africa/Nairobi")
            business.timezone = data.timezone
        if "slot_interval_minutes" in fields:
            if 60 % data.slot_interval_minutes:
                raise _validation_error("slotIntervalMinutes must divide 60 evenly (5, 10, 15, 20, 30 or 60)")
            business.slot_interval_minutes = data.slot_interval_minutes
        for name in ("buffer_minutes", "min_lead_minutes", "max_days_ahead"):
            if name in fields:
                setattr(business, name, getattr(data, name))

        await self.db.commit()
        await self.db.refresh(business)
        return booking_settings_response(business)

    # ─── staff services ──────────────────────────────────────

    async def _get_staff(self, business: Business, staff_id: str) -> Staff:
        staff = await self.db.get(Staff, self._parse_path_id(staff_id, "staffId"))
        if not staff or staff.business_id != business.id:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "staffId does not exist for this business"},
            )
        return staff

    async def staff_service_ids(self, staff_ids: list) -> dict:
        """staff id -> [service ids] for the given staff, in one query."""
        if not staff_ids:
            return {}
        rows = await self.db.execute(select(StaffService).where(StaffService.staff_id.in_(staff_ids)))
        mapping: dict = {sid: [] for sid in staff_ids}
        for row in rows.scalars():
            mapping[row.staff_id].append(row.service_id)
        return mapping

    async def my_staff_services(self, user_id: uuid_pkg.UUID, business_id: str):
        """The caller's own services at business_id, with names and prices (staff / front desk)."""
        from app.modules.business.schemas import MyStaffServiceItem, MyStaffServicesResponse

        bid = self._parse_path_id(business_id, "businessId")
        staff = (
            await self.db.execute(select(Staff).where(Staff.business_id == bid, Staff.user_id == user_id))
        ).scalars().first()
        if staff is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "You don't work at this business"})
        query = select(Service).where(Service.business_id == bid).order_by(Service.name)
        if not staff.performs_all_services:
            mapped = (await self.staff_service_ids([staff.id]))[staff.id]
            query = query.where(Service.id.in_(mapped or [uuid_pkg.uuid4()]))  # none mapped: no rows
        services = (await self.db.execute(query)).scalars().all()
        return MyStaffServicesResponse(
            staffId=staff.id,
            allServices=staff.performs_all_services,
            services=[
                MyStaffServiceItem(
                    serviceId=s.id, name=s.name, durationMinutes=s.duration_minutes,
                    price=MoneyResponse(amount=s.price_amount, currency=s.price_currency),
                )
                for s in services
            ],
        )

    async def get_staff_services(self, business_id: str, user_id: str, staff_id: str) -> StaffServicesResponse:
        business = await self._get_owned_business(business_id, user_id)
        staff = await self._get_staff(business, staff_id)
        ids = (await self.staff_service_ids([staff.id]))[staff.id]
        return StaffServicesResponse(
            staffId=staff.id, allServices=staff.performs_all_services,
            serviceIds=[] if staff.performs_all_services else sorted(ids, key=str),
        )

    async def set_staff_services(
        self, business_id: str, user_id: str, staff_id: str, data: SetStaffServicesRequest
    ) -> StaffServicesResponse:
        business = await self._get_owned_business(business_id, user_id)
        staff = await self._get_staff(business, staff_id)

        wanted = list(dict.fromkeys(data.service_ids))
        if data.all_services and wanted:
            raise _validation_error("serviceIds must be empty when allServices is true")
        if wanted:
            owned = set(
                (
                    await self.db.execute(
                        select(Service.id).where(Service.business_id == business.id, Service.id.in_(wanted))
                    )
                ).scalars()
            )
            unknown = [str(i) for i in wanted if i not in owned]
            if unknown:
                raise _validation_error(f"serviceIds contains services not in this business: {', '.join(unknown)}")

        staff.performs_all_services = data.all_services
        await self.db.execute(delete(StaffService).where(StaffService.staff_id == staff.id))
        for service_id in wanted:
            self.db.add(StaffService(staff_id=staff.id, service_id=service_id))
        await self.db.commit()
        return StaffServicesResponse(staffId=staff.id, allServices=staff.performs_all_services, serviceIds=wanted)

    # ─── team members (post-publish roster) ──────────────────
    # The invitation is the roster record: pending until accepted, then the
    # member also gets a Staff row (see respond_to_invitation).

    async def list_team_members(self, business_id: str, user_id: str) -> ListTeamMembersResponse:
        business = await self._get_owned_business(business_id, user_id)
        result = await self.db.execute(
            select(TeamInvitation)
            .where(
                TeamInvitation.business_id == business.id,
                TeamInvitation.status.in_([InvitationStatus.PENDING, InvitationStatus.ACCEPTED]),
            )
            .order_by(TeamInvitation.sent_at)
        )
        return ListTeamMembersResponse(members=[team_member_response(i) for i in result.scalars()])

    async def add_team_member(self, business_id: str, user_id: str, data: AddTeamMemberRequest) -> TeamMemberResponse:
        business = await self._get_owned_business(business_id, user_id)
        invitation = await self._create_invitation(
            business,
            user_id,
            data,
            name=data.name,
            commission_percent=data.commission_percent,
            working_days=self._normalise_days(data.working_days),
        )
        return team_member_response(invitation)

    async def update_team_member(
        self, business_id: str, user_id: str, invitation_id: str, data: UpdateTeamMemberRequest
    ) -> TeamMemberResponse:
        business = await self._get_owned_business(business_id, user_id)
        invitation = await self._get_member(business, invitation_id)

        # Once accepted, Staff is the source of truth; keep the roster record in step.
        staff = await self.db.get(Staff, invitation.staff_id) if invitation.staff_id else None
        fields = data.model_fields_set
        if "commission_percent" in fields:
            if data.commission_percent is None:
                raise _validation_error("commissionPercent must be between 0 and 100")
            invitation.commission_percent = data.commission_percent
            if staff:
                staff.commission_percent = data.commission_percent
        if "working_days" in fields:
            invitation.working_days = self._normalise_days(data.working_days)
            if staff:
                staff.working_days = invitation.working_days

        await self.db.commit()
        await self.db.refresh(invitation)
        return team_member_response(invitation)

    async def remove_team_member(self, business_id: str, user_id: str, invitation_id: str) -> None:
        """Cancels a pending invitation, or removes an accepted member from the team."""
        business = await self._get_owned_business(business_id, user_id)
        invitation = await self._get_member(business, invitation_id)

        if invitation.staff_id:
            await self.db.execute(
                delete(Staff).where(Staff.business_id == business.id, Staff.id == invitation.staff_id)
            )
        elif invitation.status == InvitationStatus.ACCEPTED and invitation.user_id:
            # Accepted before invitations were linked to Staff (pre-006 rows the backfill missed).
            await self.db.execute(
                delete(Staff).where(Staff.business_id == business.id, Staff.user_id == invitation.user_id)
            )
        await self.db.delete(invitation)
        await self.db.commit()

    async def _get_member(self, business: Business, invitation_id: str) -> TeamInvitation:
        invitation = await self.db.get(TeamInvitation, self._parse_path_id(invitation_id, "invitationId"))
        if (
            not invitation
            or invitation.business_id != business.id
            or invitation.status not in (InvitationStatus.PENDING, InvitationStatus.ACCEPTED)
        ):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "invitationId does not exist for this business"},
            )
        return invitation

    @staticmethod
    def _normalise_days(days: Optional[list]) -> Optional[list]:
        # Dedupe and store in week order.
        return None if days is None else [d for d in _WEEKDAYS if d in set(days)]

    # ─── publish-business ────────────────────────────────────

    async def publish_business(self, business_id: str, user_id: str, data: PublishBusinessRequest) -> PublishBusinessResponse:
        business = await self._get_business_with_relations(business_id, user_id)

        if data.visible:
            if not business.working_hours:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "workingHours must be set before publishing"},
                )
            if not business.service_categories:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "at least one service category is required"},
                )
            has_services = any(cat.services for cat in business.service_categories)
            if not has_services:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "at least one service is required"},
                )
            if not business.policies:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "policies must be set before publishing"},
                )
            if (
                not business.payment_destination
                or business.payment_destination.verification_status != VerificationStatus.VERIFIED
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "paymentDestination must be verified before publishing"},
                )

            if business.team_mode != TeamMode.TEAM:
                await self._ensure_owner_staff(business)

        business.status = BusinessStatus.PUBLISHED if data.visible else BusinessStatus.DRAFT
        business.published_at = datetime.now(timezone.utc) if data.visible else None
        business.onboarding_step = OnboardingStep.REVIEW_PUBLISH
        await self.db.commit()

        return PublishBusinessResponse(
            businessId=business.id,
            status=business.status,
            visible=data.visible,
            publishedAt=business.published_at,
        )

    async def _ensure_owner_staff(self, business: Business) -> None:
        """The owner's own Staff row (role "owner"), so a solo business has someone to book."""
        existing = await self.db.execute(
            select(Staff.id).where(Staff.business_id == business.id, Staff.user_id == business.owner_id)
        )
        if existing.first():
            return
        owner = await self.db.get(User, business.owner_id)
        owner_name = ""
        if owner:
            owner_name = " ".join(p for p in (owner.first_name, owner.last_name) if p) or owner.email or "Owner"
        self.db.add(
            Staff(
                business_id=business.id,
                user_id=business.owner_id,
                name=owner_name or "Owner",
                role="owner",
                specialties=[],
                portfolio=[],
            )
        )

    # ─── My Businesses ────────────────────────────────────────

    async def list_my_businesses(self, user_id: uuid_pkg.UUID):
        from app.modules.business.schemas import ListMyBusinessesResponse, MyBusinessResponse
        owned = await self.db.execute(
            select(Business)
            .where(Business.owner_id == user_id)
            .order_by(Business.created_at.asc())
        )
        owned_businesses = owned.scalars().all()
        memberships = (
            await self.db.execute(
                select(Staff, Business)
                .join(Business, Business.id == Staff.business_id)
                .where(Staff.user_id == user_id)
                .order_by(Staff.created_at.asc())
            )
        ).all()
        # Owners also get a Staff row (role "owner") on their own business when they work solo.
        my_staff = {business.id: staff for staff, business in memberships}

        items = [
            MyBusinessResponse(
                businessId=b.id, name=b.name, status=b.status, role="owner",
                staffId=my_staff[b.id].id if b.id in my_staff else None,
            )
            for b in owned_businesses
        ]
        seen = {item.business_id for item in items}
        for staff, business in memberships:
            if business.id in seen:
                continue
            seen.add(business.id)
            items.append(MyBusinessResponse(
                businessId=business.id, name=business.name, status=business.status, role=staff.role, staffId=staff.id,
            ))
        return ListMyBusinessesResponse(businesses=items)

    # ─── Team Invitations (received) ──────────────────────────

    async def list_my_invitations(self, user_id: str):
        from app.modules.business.schemas import ListMyInvitationsResponse, MyInvitationResponse
        user = await self.db.get(User, user_id)
        mine = invitations_for(user) if user else None
        if mine is None:
            return ListMyInvitationsResponse(invitations=[])
        result = await self.db.execute(
            select(TeamInvitation)
            .where(TeamInvitation.status == InvitationStatus.PENDING, mine)
            .order_by(TeamInvitation.sent_at.desc())
        )
        invitations = result.scalars().all()
        items = []
        for inv in invitations:
            biz = await self.db.get(Business, inv.business_id)
            items.append(MyInvitationResponse(
                invitationId=inv.id,
                businessId=inv.business_id,
                businessName=biz.name if biz else "",
                role=inv.role,
                status=inv.status,
                sentAt=inv.sent_at,
            ))
        return ListMyInvitationsResponse(invitations=items)

    async def respond_to_invitation(self, invitation_id: str, user_id: str, data):
        from app.modules.business.schemas import RespondToInvitationResponse
        invitation = await self.db.get(TeamInvitation, self._parse_path_id(invitation_id, "invitationId"))
        if not invitation:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No pending invitation found for this id"})
        if invitation.status != InvitationStatus.PENDING:
            raise HTTPException(status_code=409, detail={"error": "already_responded", "message": "This invitation has already been accepted or declined"})
        user = await self.db.get(User, user_id)
        mine = invitations_for(user) if user else None
        if mine is None or not (
            await self.db.execute(select(TeamInvitation.id).where(TeamInvitation.id == invitation.id, mine))
        ).first():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No pending invitation found for this id"})
        if data.action == "accept":
            invitation.status = InvitationStatus.ACCEPTED
            invitation.user_id = user_id

            staff = (
                await self.db.execute(
                    select(Staff).where(
                        Staff.business_id == invitation.business_id,
                        Staff.user_id == uuid_pkg.UUID(str(user_id)),
                    )
                )
            ).scalar_one_or_none()
            if not staff:
                member_name = (
                    " ".join(p for p in (user.first_name, user.last_name) if p)
                    or invitation.name
                    or user.email
                    or "Team member"
                )
                staff = Staff(
                    id=uuid_pkg.uuid4(),
                    business_id=invitation.business_id,
                    user_id=uuid_pkg.UUID(str(user_id)),
                    name=member_name,
                    role=invitation.role,
                    specialties=[],
                    portfolio=[],
                    # Front desk isn't bookable until the owner gives them services
                    # (PUT /businesses/{id}/staff/{staffId}/services).
                    performs_all_services=invitation.role != "front_desk",
                )
                self.db.add(staff)
            # Staff becomes this member's bookable identity (docs/booking/PLAN.md, D3).
            staff.working_days = invitation.working_days
            staff.commission_percent = invitation.commission_percent
            invitation.staff_id = staff.id
        else:
            invitation.status = InvitationStatus.DECLINED
        await self.db.commit()
        return RespondToInvitationResponse(
            invitationId=invitation.id,
            status=invitation.status,
        )

    # ─── Discovery: search ─────────────────────────────────────

    async def search_businesses(
        self,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        query: Optional[str] = None,
        category: Optional[str] = None,
        price_max: Optional[int] = None,
        rating_min: Optional[float] = None,
        radius_km: Optional[float] = None,
    ) -> SearchResponse:
        result = await self.db.execute(
            select(Business)
            .where(Business.status == BusinessStatus.PUBLISHED)
            .options(
                selectinload(Business.photos),
                selectinload(Business.service_categories).selectinload(ServiceCategory.services),
                selectinload(Business.reviews),
            )
        )
        businesses = result.scalars().all()

        has_coords = lat is not None and lng is not None
        q = query.strip().lower() if query else ""

        rows = []
        for biz in businesses:
            if category and category not in (biz.categories or []):
                continue

            services = [s for cat in (biz.service_categories or []) for s in (cat.services or [])]

            if q:
                haystack = " ".join([biz.name, *[s.name for s in services]]).lower()
                if q not in haystack:
                    continue

            if price_max is not None and not any(s.price_amount <= price_max for s in services):
                continue

            reviews = list(biz.reviews or [])
            rating = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else 0.0
            if rating_min is not None and rating < rating_min:
                continue

            loc = biz.location or {}
            biz_lat = loc.get("lat", 0.0)
            biz_lng = loc.get("lng", 0.0)
            distance = round(haversine_km(lat, lng, biz_lat, biz_lng), 1) if has_coords else 0.0
            if has_coords and radius_km is not None and distance > radius_km:
                continue

            summary = business_summary(biz, distance)
            published_ts = biz.published_at.timestamp() if biz.published_at else 0.0
            rows.append((distance, published_ts, summary))

        if has_coords:
            rows.sort(key=lambda r: r[0])
        else:
            rows.sort(key=lambda r: r[1], reverse=True)

        return SearchResponse(businesses=[r[2] for r in rows])

    # ─── Discovery: profile ────────────────────────────────────

    async def get_business_profile(self, business_id: str) -> BusinessProfileResponse:
        not_found = HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "No published business found for this id"},
        )
        try:
            bid = uuid_pkg.UUID(str(business_id))
        except (ValueError, TypeError, AttributeError):
            raise not_found

        result = await self.db.execute(
            select(Business)
            .where(Business.id == bid, Business.status == BusinessStatus.PUBLISHED)
            .options(
                selectinload(Business.photos),
                selectinload(Business.working_hours),
                selectinload(Business.service_categories).selectinload(ServiceCategory.services),
                selectinload(Business.policies),
                selectinload(Business.staff),
                selectinload(Business.reviews),
            )
        )
        business = result.scalar_one_or_none()
        if not business:
            raise not_found

        loc = business.location or {}
        policies = business.policies

        reviews_sorted = sorted(
            business.reviews or [], key=lambda r: r.created_at, reverse=True
        )
        rating = (
            round(sum(r.rating for r in reviews_sorted) / len(reviews_sorted), 1)
            if reviews_sorted
            else 0.0
        )

        photo_urls = [p.url for p in sorted(
            business.photos or [],
            key=lambda p: (not p.is_cover, p.created_at),
        )]

        working_hours = weekly_hours_response(
            business.working_hours.weekly_hours if business.working_hours else None
        )

        profile_policies = ProfilePoliciesResponse(
            deposit=deposit_response(policies),
            cancellation=CancellationPolicyResponse(
                freeCancellationHours=policies.cancellation_free_hours if policies else 24,
                lateFeePercent=policies.cancellation_late_fee_percent if policies else 50,
            ),
            noShow=NoShowPolicyResponse(
                feePercent=policies.no_show_fee_percent if policies else 100
            ),
        )

        services = [
            ProfileServiceResponse(
                serviceId=s.id,
                categoryName=cat.name,
                name=s.name,
                durationMinutes=s.duration_minutes,
                price=MoneyResponse(amount=s.price_amount, currency=s.price_currency),
            )
            for cat in (business.service_categories or [])
            for s in (cat.services or [])
        ]

        staff_rows = sorted(business.staff or [], key=lambda st: st.created_at)
        mapped = await self.staff_service_ids([st.id for st in staff_rows if not st.performs_all_services])
        staff = [
            StaffMemberResponse(
                staffId=st.id,
                name=st.name,
                role=st.role,
                workingDays=st.working_days,
                allServices=st.performs_all_services,
                serviceIds=sorted(mapped.get(st.id, []), key=str),
                rating=st.rating,
                avatarUrl=st.avatar_url,
                bio=st.bio or "",
                specialties=st.specialties or [],
                portfolio=st.portfolio or [],
            )
            for st in staff_rows
        ]

        review_items = [
            ReviewResponse(
                reviewId=r.id,
                authorName=r.author_name,
                rating=r.rating,
                text=r.text or "",
                createdAt=r.created_at,
            )
            for r in reviews_sorted
        ]

        return BusinessProfileResponse(
            businessId=business.id,
            name=business.name,
            description=business.description or "",
            categories=business.categories or [],
            photos=photo_urls,
            rating=rating,
            reviewCount=len(reviews_sorted),
            address=loc.get("address", ""),
            lat=loc.get("lat", 0.0),
            lng=loc.get("lng", 0.0),
            depositSummary=derive_deposit_summary(policies),
            cancellationSummary=derive_cancellation_summary(policies),
            workingHours=working_hours,
            policies=profile_policies,
            amenities=business.amenities or [],
            services=services,
            staff=staff,
            reviews=review_items,
        )
