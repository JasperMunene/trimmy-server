from datetime import datetime, timezone
from typing import Optional
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
    Service,
    ServiceCategory,
    TeamInvitation,
    TeamMode,
    VerificationStatus,
    WorkingHours,
)
from app.modules.business.schemas import (
    CreateBusinessRequest,
    CreateBusinessResponse,
    CreateServiceCategoryRequest,
    CreateServiceRequest,
    GetBusinessResponse,
    InviteTeamMemberRequest,
    ListServiceCategoriesResponse,
    LocationResponse,
    MoneyResponse,
    PaymentDestinationResponse,
    PhotoResponse,
    PoliciesResponse,
    PublishBusinessRequest,
    PublishBusinessResponse,
    ServiceCategoryResponse,
    ServiceResponse,
    SetPaymentDestinationRequest,
    SetPoliciesRequest,
    SetTeamModeRequest,
    TeamInvitationResponse,
    TeamModeResponse,
    WorkingHoursResponse,
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
