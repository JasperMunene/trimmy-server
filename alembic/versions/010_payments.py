"""payments (one row per STK push)

Revision ID: 010
Revises: 009
Create Date: 2026-09-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="deposit"),
        sa.Column("provider", sa.String(20), nullable=False, server_default="mpesa"),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KES"),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("destination_type", sa.String(20), nullable=False),
        sa.Column("party_b", sa.String(20), nullable=False),
        sa.Column("account_reference", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.String(30), nullable=True),
        sa.Column("checkout_request_id", sa.String(100), nullable=True, unique=True),
        sa.Column("merchant_request_id", sa.String(100), nullable=True),
        sa.Column("mpesa_receipt", sa.String(30), nullable=True),
        sa.Column("result_code", sa.Integer(), nullable=True),
        sa.Column("result_desc", sa.Text(), nullable=True),
        sa.Column("raw_callback", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payments_booking_created", "payments", ["booking_id", "created_at"])
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("payments")
