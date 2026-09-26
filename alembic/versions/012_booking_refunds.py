"""booking cancellation fee and refund tracking

Revision ID: 012
Revises: 011
Create Date: 2026-09-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("cancellation_fee", sa.Integer()),
    ("refund_amount", sa.Integer()),
    ("refund_status", sa.String(20)),
    ("refunded_at", sa.DateTime(timezone=True)),
    ("refund_reference", sa.String(50)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("bookings", sa.Column(name, type_, nullable=True))
    op.create_index("ix_bookings_business_refund", "bookings", ["business_id", "refund_status"])


def downgrade() -> None:
    op.drop_index("ix_bookings_business_refund", table_name="bookings")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("bookings", name)
