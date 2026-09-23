from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Optional
import uuid as uuid_pkg
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    TeamInvitation,
    TeamMode,
    VerificationStatus,
    WorkingHours,
)
from app.modules.business.schemas import (
    BusinessProfileResponse,
    BusinessSummaryResponse,
    CancellationPolicyResponse,
    CreateBusinessRequest,
    CreateBusinessResponse,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    DepositRuleResponse,
    GetBusinessResponse,
    InviteTeamMemberRequest,
    ListServiceCategoriesResponse,
    LocationResponse,
    MoneyResponse,
    NoShowPolicyResponse,
    PaymentDestinationResponse,
    PhotoResponse,
    PoliciesResponse,
    ProfilePoliciesResponse,
    ProfileServiceResponse,
    PublishBusinessRequest,
    PublishBusinessResponse,
    ReviewResponse,
    SearchResponse,
    ServiceCategoryResponse,
    ServiceResponse,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetTeamModeRequest,
    StaffMemberResponse,
    TeamInvitationResponse,
    TeamModeResponse,
    WorkingHoursResponse,
)


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
    amount = policies.deposit_amount or 0
    if policies.deposit_type == "percent":
        return f"{amount}% deposit required"
    currency = policies.deposit_currency or "KES"
    return f"{currency} {amount} deposit required"


def derive_cancellation_summary(policies: Optional[BusinessPolicies]) -> str:
    if not policies:
        return "Free cancellation up to 24h before, 50% fee after"
    return (
        f"Free cancellation up to {policies.cancellation_free_hours}h before, "
        f"{policies.cancellation_late_fee_percent}% fee after"
    )


class BusinessService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_owned_business(self, business_id: str, user_id: str) -> Business:
        result = await self.db.execute(
            select(Business).where(
                Business.id == business_id,
                Business.owner_id == user_id,
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
                Business.id == business_id,
                Business.owner_id == user_id,
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
            categories=business.categories,
            phone=business.phone,
            location=LocationResponse(**business.location),
            status=business.status,
            onboardingStep=business.onboarding_step,
            createdAt=business.created_at,
        )

    # ─── get-business-draft ──────────────────────────────────

    async def get_business(self, business_id: str, user_id: str) -> GetBusinessResponse:
        business = await self._get_business_with_relations(business_id, user_id)

        photos = [
            PhotoResponse(
                photoId=p.id,
                businessId=p.business_id,
                url=p.url,
                isCover=p.is_cover,
                createdAt=p.created_at,
            )
            for p in (business.photos or [])
        ]

        working_hours = None
        if business.working_hours:
            working_hours = WorkingHoursResponse(
                businessId=business.working_hours.business_id,
                weeklyHours=business.working_hours.weekly_hours,
                onboardingStep=business.onboarding_step,
            )

        service_categories = []
        for cat in (business.service_categories or []):
            services = [
                ServiceResponse(
                    serviceId=s.id,
                    businessId=s.business_id,
                    categoryId=s.category_id,
                    name=s.name,
                    durationMinutes=s.duration_minutes,
                    price=MoneyResponse(amount=s.price_amount, currency=s.price_currency),
                    createdAt=s.created_at,
                )
                for s in cat.services
            ]
            service_categories.append(
                ServiceCategoryResponse(
                    categoryId=cat.id,
                    businessId=cat.business_id,
                    name=cat.name,
                    createdAt=cat.created_at,
                ).model_dump()
            )
            service_categories[-1]["services"] = [s.model_dump() for s in services]

        policies = None
        if business.policies:
            p = business.policies
            policies = PoliciesResponse(
                businessId=p.business_id,
                deposit={
                    "required": p.deposit_required,
                    "type": p.deposit_type,
                    "amount": {"amount": p.deposit_amount or 0, "currency": p.deposit_currency or "KES"},
                    "appliesTo": p.deposit_applies_to,
                },
                cancellation={
                    "freeCancellationHours": p.cancellation_free_hours,
                    "lateFeePercent": p.cancellation_late_fee_percent,
                },
                noShow={"feePercent": p.no_show_fee_percent},
                onboardingStep=business.onboarding_step,
            )

        payment_dest = None
        if business.payment_destination:
            pd = business.payment_destination
            payment_dest = PaymentDestinationResponse(
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

        return GetBusinessResponse(
            businessId=business.id,
            name=business.name,
            categories=business.categories,
            phone=business.phone,
            description=business.description,
            amenities=business.amenities,
            location=LocationResponse(**business.location) if business.location else None,
            photos=photos,
            workingHours=working_hours,
            serviceCategories=service_categories,
            policies=policies,
            paymentDestination=payment_dest,
            teamMode=business.team_mode,
            status=business.status,
            onboardingStep=business.onboarding_step,
        )

    # ─── upload-business-photo ───────────────────────────────

    async def upload_photo(self, business_id: str, user_id: str, url: str, is_cover: bool) -> PhotoResponse:
        business = await self._get_owned_business(business_id, user_id)

        photo = BusinessPhoto(
            business_id=business.id,
            url=url,
            is_cover=is_cover,
        )
        self.db.add(photo)
        business.onboarding_step = OnboardingStep.PHOTOS
        await self.db.commit()
        await self.db.refresh(photo)

        return PhotoResponse(
            photoId=photo.id,
            businessId=photo.business_id,
            url=photo.url,
            isCover=photo.is_cover,
            createdAt=photo.created_at,
        )

    # ─── set-working-hours ───────────────────────────────────

    async def set_working_hours(self, business_id: str, user_id: str, weekly_hours: dict) -> WorkingHoursResponse:
        business = await self._get_owned_business(business_id, user_id)

        for day, hours in weekly_hours.items():
            if hours is not None:
                if hours.get("open") >= hours.get("close"):
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "validation_error", "message": "close time must be after open time"},
                    )

        existing = await self.db.execute(
            select(WorkingHours).where(WorkingHours.business_id == business.id)
        )
        wh = existing.scalar_one_or_none()
        if wh:
            wh.weekly_hours = weekly_hours
        else:
            wh = WorkingHours(business_id=business.id, weekly_hours=weekly_hours)
            self.db.add(wh)

        business.onboarding_step = OnboardingStep.WORKING_HOURS
        await self.db.commit()
        await self.db.refresh(wh)

        return WorkingHoursResponse(
            businessId=business.id,
            weeklyHours=weekly_hours,
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
        categories = result.scalars().all()

        output = []
        for cat in categories:
            services = [
                {
                    "serviceId": str(s.id),
                    "businessId": str(s.business_id),
                    "categoryId": str(s.category_id),
                    "name": s.name,
                    "durationMinutes": s.duration_minutes,
                    "price": {"amount": s.price_amount, "currency": s.price_currency},
                }
                for s in cat.services
            ]
            output.append({
                "categoryId": str(cat.id),
                "businessId": str(cat.business_id),
                "name": cat.name,
                "services": services,
            })

        return ListServiceCategoriesResponse(categories=output)

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

        return ServiceResponse(
            serviceId=service.id,
            businessId=service.business_id,
            categoryId=service.category_id,
            name=service.name,
            durationMinutes=service.duration_minutes,
            price=MoneyResponse(amount=service.price_amount, currency=service.price_currency),
            createdAt=service.created_at,
        )

    # ─── set-policies ────────────────────────────────────────

    async def set_policies(self, business_id: str, user_id: str, data: SetPoliciesRequest) -> PoliciesResponse:
        business = await self._get_owned_business(business_id, user_id)

        existing = await self.db.execute(
            select(BusinessPolicies).where(BusinessPolicies.business_id == business.id)
        )
        policies = existing.scalar_one_or_none()
        if policies:
            policies.deposit_required = data.deposit.required
            policies.deposit_type = data.deposit.type
            policies.deposit_amount = data.deposit.amount.amount
            policies.deposit_currency = data.deposit.amount.currency
            policies.deposit_applies_to = data.deposit.applies_to
            policies.cancellation_free_hours = data.cancellation.free_cancellation_hours
            policies.cancellation_late_fee_percent = data.cancellation.late_fee_percent
            policies.no_show_fee_percent = data.no_show.fee_percent
        else:
            policies = BusinessPolicies(
                business_id=business.id,
                deposit_required=data.deposit.required,
                deposit_type=data.deposit.type,
                deposit_amount=data.deposit.amount.amount,
                deposit_currency=data.deposit.amount.currency,
                deposit_applies_to=data.deposit.applies_to,
                cancellation_free_hours=data.cancellation.free_cancellation_hours,
                cancellation_late_fee_percent=data.cancellation.late_fee_percent,
                no_show_fee_percent=data.no_show.fee_percent,
            )
            self.db.add(policies)

        business.onboarding_step = OnboardingStep.POLICIES
        await self.db.commit()

        return PoliciesResponse(
            businessId=business.id,
            deposit={
                "required": policies.deposit_required,
                "type": policies.deposit_type,
                "amount": {"amount": policies.deposit_amount, "currency": policies.deposit_currency},
                "appliesTo": policies.deposit_applies_to,
            },
            cancellation={
                "freeCancellationHours": policies.cancellation_free_hours,
                "lateFeePercent": policies.cancellation_late_fee_percent,
            },
            noShow={"feePercent": policies.no_show_fee_percent},
            onboardingStep=business.onboarding_step,
        )

    # ─── set-payment-destination ─────────────────────────────

    async def set_payment_destination(self, business_id: str, user_id: str, data: SetPaymentDestinationRequest) -> PaymentDestinationResponse:
        business = await self._get_owned_business(business_id, user_id)

        if data.type == PaymentDestinationType.MPESA_TILL and not data.till_number:
            raise HTTPException(status_code=400, detail={"error": "validation_error", "message": "tillNumber is required for type mpesa_till"})
        if data.type == PaymentDestinationType.MPESA_PAYBILL:
            if not data.paybill_number or not data.paybill_account_number:
                raise HTTPException(status_code=400, detail={"error": "validation_error", "message": "paybillNumber and paybillAccountNumber are required for type mpesa_paybill"})
        if data.type == PaymentDestinationType.BANK_ACCOUNT:
            if not data.bank_name or not data.bank_shortcode or not data.bank_account_number:
                raise HTTPException(status_code=400, detail={"error": "validation_error", "message": "bankName, bankShortcode, and bankAccountNumber are required for type bank_account"})

        existing = await self.db.execute(
            select(PaymentDestination).where(PaymentDestination.business_id == business.id)
        )
        pd = existing.scalar_one_or_none()
        if pd:
            pd.type = data.type
            pd.till_number = data.till_number
            pd.paybill_number = data.paybill_number
            pd.paybill_account_number = data.paybill_account_number
            pd.bank_name = data.bank_name
            pd.bank_shortcode = data.bank_shortcode
            pd.bank_account_number = data.bank_account_number
            pd.verification_status = VerificationStatus.VERIFIED
        else:
            pd = PaymentDestination(
                business_id=business.id,
                type=data.type,
                till_number=data.till_number,
                paybill_number=data.paybill_number,
                paybill_account_number=data.paybill_account_number,
                bank_name=data.bank_name,
                bank_shortcode=data.bank_shortcode,
                bank_account_number=data.bank_account_number,
                verification_status=VerificationStatus.VERIFIED,
            )
            self.db.add(pd)

        business.onboarding_step = OnboardingStep.PAYMENT_DESTINATION
        await self.db.commit()

        return PaymentDestinationResponse(
            businessId=business.id,
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

    # ─── set-team-mode ───────────────────────────────────────

    async def set_team_mode(self, business_id: str, user_id: str, data: SetTeamModeRequest) -> TeamModeResponse:
        business = await self._get_owned_business(business_id, user_id)
        business.team_mode = data.team_mode
        business.onboarding_step = OnboardingStep.TEAM_MODE
        await self.db.commit()

        return TeamModeResponse(
            businessId=business.id,
            teamMode=business.team_mode,
            onboardingStep=business.onboarding_step,
        )

    # ─── invite-team-member ──────────────────────────────────

    async def invite_team_member(self, business_id: str, user_id: str, data: InviteTeamMemberRequest) -> TeamInvitationResponse:
        business = await self._get_owned_business(business_id, user_id)

        if not data.email and not data.phone:
            raise HTTPException(
                status_code=400,
                detail={"error": "validation_error", "message": "email or phone is required"},
            )

        if data.email:
            existing = await self.db.execute(
                select(TeamInvitation).where(
                    TeamInvitation.business_id == business.id,
                    TeamInvitation.email == data.email,
                    TeamInvitation.status == InvitationStatus.PENDING,
                )
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already_invited", "message": "This email already has a pending invitation"},
                )

        if data.phone:
            existing = await self.db.execute(
                select(TeamInvitation).where(
                    TeamInvitation.business_id == business.id,
                    TeamInvitation.phone == data.phone,
                    TeamInvitation.status == InvitationStatus.PENDING,
                )
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=409,
                    detail={"error": "already_invited", "message": "This phone already has a pending invitation"},
                )

        invitation = TeamInvitation(
            business_id=business.id,
            email=data.email,
            phone=data.phone,
            role=data.role,
            status=InvitationStatus.PENDING,
            invited_by=user_id,
        )
        self.db.add(invitation)
        await self.db.commit()
        await self.db.refresh(invitation)

        return TeamInvitationResponse(
            invitationId=invitation.id,
            businessId=invitation.business_id,
            email=invitation.email,
            phone=invitation.phone,
            role=invitation.role,
            status=invitation.status,
            sentAt=invitation.sent_at,
        )

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
            if not business.payment_destination:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "incomplete_setup", "message": "paymentDestination must be verified before publishing"},
                )

            if business.team_mode != TeamMode.TEAM:
                existing_staff = await self.db.execute(
                    select(Staff).where(
                        Staff.business_id == business.id,
                        Staff.user_id == business.owner_id,
                    )
                )
                if not existing_staff.scalar_one_or_none():
                    owner = await self.db.get(User, business.owner_id)
                    owner_name = ""
                    if owner:
                        owner_name = " ".join(
                            p for p in (owner.first_name, owner.last_name) if p
                        ) or owner.email or "Owner"
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

    # ─── Team Invitations (received) ──────────────────────────

    async def list_my_invitations(self, user_id: str):
        from app.modules.business.schemas import ListMyInvitationsResponse, MyInvitationResponse
        user = await self.db.get(User, user_id)
        if not user or not user.phone:
            return ListMyInvitationsResponse(invitations=[])
        result = await self.db.execute(
            select(TeamInvitation)
            .where(
                TeamInvitation.status == InvitationStatus.PENDING,
                TeamInvitation.phone == user.phone,
            )
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
        invitation = await self.db.get(TeamInvitation, invitation_id)
        if not invitation:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No pending invitation found for this id"})
        if invitation.status != InvitationStatus.PENDING:
            raise HTTPException(status_code=409, detail={"error": "already_responded", "message": "This invitation has already been accepted or declined"})
        user = await self.db.get(User, user_id)
        if not user or not user.phone or user.phone != invitation.phone:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No pending invitation found for this id"})
        if data.action == "accept":
            invitation.status = InvitationStatus.ACCEPTED
            invitation.user_id = user_id

            existing_staff = await self.db.execute(
                select(Staff).where(
                    Staff.business_id == invitation.business_id,
                    Staff.user_id == uuid_pkg.UUID(str(user_id)),
                )
            )
            if not existing_staff.scalar_one_or_none():
                member_name = (
                    " ".join(p for p in (user.first_name, user.last_name) if p)
                    or user.email
                    or "Team member"
                )
                self.db.add(
                    Staff(
                        business_id=invitation.business_id,
                        user_id=uuid_pkg.UUID(str(user_id)),
                        name=member_name,
                        role=invitation.role,
                        specialties=[],
                        portfolio=[],
                    )
                )
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

            photo_urls = [p.url for p in sorted(
                biz.photos or [],
                key=lambda p: (not p.is_cover, p.created_at),
            )]
            if services:
                cheapest = min(services, key=lambda s: s.price_amount)
                starting_price = MoneyResponse(amount=cheapest.price_amount, currency=cheapest.price_currency)
            else:
                starting_price = MoneyResponse(amount=0, currency="KES")

            summary = BusinessSummaryResponse(
                businessId=biz.id,
                name=biz.name,
                categories=biz.categories or [],
                thumbnailUrl=photo_urls[0] if photo_urls else "",
                photos=photo_urls,
                address=loc.get("address", ""),
                lat=biz_lat,
                lng=biz_lng,
                distanceKm=distance,
                startingPrice=starting_price,
                rating=rating,
                reviewCount=len(reviews),
            )
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

        raw_hours = business.working_hours.weekly_hours if business.working_hours else {}
        working_hours = {day: raw_hours.get(day) for day in _WEEKDAYS}

        profile_policies = ProfilePoliciesResponse(
            deposit=DepositRuleResponse(
                required=policies.deposit_required if policies else False,
                type=policies.deposit_type if policies else None,
                amount=MoneyResponse(
                    amount=(policies.deposit_amount or 0) if policies else 0,
                    currency=(policies.deposit_currency or "KES") if policies else "KES",
                ),
                appliesTo=policies.deposit_applies_to if policies else None,
            ),
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

        staff = [
            StaffMemberResponse(
                staffId=st.id,
                name=st.name,
                role=st.role,
                rating=st.rating,
                avatarUrl=st.avatar_url,
                bio=st.bio or "",
                specialties=st.specialties or [],
                portfolio=st.portfolio or [],
            )
            for st in sorted(business.staff or [], key=lambda st: st.created_at)
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
