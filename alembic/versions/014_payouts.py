"""payouts, and each booking's staff commission

Revision ID: 014
Revises: 013
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("staff_commission", sa.Integer(), nullable=True))
    op.create_table(
        "payouts",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True),
        sa.Column("staff_name", sa.Text(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KES"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("requested_by", UUID(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("responded_by", UUID(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_payouts_business_status", "payouts", ["business_id", "status"])
    op.create_index("ix_payouts_staff", "payouts", ["staff_id"])


def downgrade() -> None:
    op.drop_table("payouts")
    op.drop_column("bookings", "staff_commission")
