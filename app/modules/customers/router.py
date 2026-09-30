from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_verified_user
from app.modules.auth.models import User
from app.modules.customers.schemas import CustomerResponse, ListCustomersResponse, SaveCustomerRequest
from app.modules.customers.service import CustomersService

router = APIRouter(prefix="/businesses", tags=["Customers"])


@router.get("/{business_id}/customers", response_model=ListCustomersResponse)
async def list_customers(
    business_id: str,
    query: Optional[str] = Query(None, description="Search name, email or phone"),
    limit: int = Query(100, description="1-500"),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner or front desk: everyone who has booked (with a phone) or been saved by hand, A-Z."""
    return await CustomersService(db).list_customers(current_user, business_id, query, limit, offset)


@router.post("/{business_id}/customers", status_code=status.HTTP_200_OK, response_model=CustomerResponse)
async def save_customer(
    business_id: str,
    data: SaveCustomerRequest,
    current_user: User = Depends(get_current_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a customer. With customerId: update it. Without: update whoever has
    this phone, or create a new customer."""
    return await CustomersService(db).save(current_user, business_id, data)
