"""business_customers, backfilled from bookings

Revision ID: 013
Revises: 012
Create Date: 2026-09-30

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    table = op.create_table(
        "business_customers",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_business_customers_business", "business_customers", ["business_id"])
    op.create_index(
        "uq_business_customers_phone", "business_customers", ["business_id", "phone"], unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"), sqlite_where=sa.text("phone IS NOT NULL"),
    )

    # Backfill: one customer per (business, phone) from existing bookings, latest details win.
    from app.core.phone_utils import InvalidPhoneNumberError, normalise_phone

    rows = op.get_bind().execute(sa.text(
        "SELECT business_id, customer_phone, customer_name, customer_email, created_at FROM bookings "
        "WHERE customer_phone IS NOT NULL AND customer_phone <> '' ORDER BY created_at"
    )).fetchall()
    latest: dict = {}
    for business_id, phone, name, email, created in rows:
        if isinstance(created, str):  # SQLite hands back text
            created = datetime.fromisoformat(created)
        try:
            phone = normalise_phone(phone)
        except InvalidPhoneNumberError:
            continue
        first = latest.get((business_id, phone), {}).get("created_at", created)
        latest[(business_id, phone)] = {"name": name, "email": email, "created_at": first}
    now = datetime.now(timezone.utc)
    if latest:
        op.bulk_insert(table, [
            {"id": uuid.uuid4(), "business_id": business_id, "name": v["name"], "phone": phone, "email": v["email"],
             "created_at": v["created_at"] or now, "updated_at": now}
            for (business_id, phone), v in latest.items()
        ])


def downgrade() -> None:
    op.drop_table("business_customers")
