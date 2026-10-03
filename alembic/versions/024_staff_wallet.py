"""staff wallet: bookings.paid_at and wallet_adjustments

Revision ID: 024
Revises: 023
Create Date: 2026-10-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    # Already-paid bookings: when their last successful payment settled, else their last update.
    op.execute(
        "UPDATE bookings SET paid_at = COALESCE("
        "(SELECT MAX(p.settled_at) FROM payments p WHERE p.booking_id = bookings.id AND p.status = 'succeeded'),"
        " updated_at) WHERE payment_status = 'paid'"
    )
    op.create_table(
        "wallet_adjustments",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True),
        sa.Column("staff_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KES"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("voided_by", UUID(), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wallet_adjustments_staff", "wallet_adjustments", ["staff_id"])


def downgrade() -> None:
    op.drop_table("wallet_adjustments")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("paid_at")
