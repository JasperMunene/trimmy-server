"""bookings, booking_services, booking_events

Revision ID: 009
Revises: 008
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bookings",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("reference", sa.String(12), nullable=False, unique=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="online"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("local_time", sa.String(5), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KES"),
        sa.Column("deposit_amount", sa.Integer(), nullable=True),
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="unpaid"),
        sa.Column("customer_name", sa.Text(), nullable=False),
        sa.Column("customer_phone", sa.Text(), nullable=True),
        sa.Column("customer_email", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_bookings_business_starts", "bookings", ["business_id", "starts_at"])
    op.create_index("ix_bookings_staff_starts", "bookings", ["staff_id", "starts_at"])
    op.create_index("ix_bookings_customer_starts", "bookings", ["customer_id", "starts_at"])
    op.create_index("ix_bookings_status_expires", "bookings", ["status", "expires_at"])

    if op.get_bind().dialect.name == "postgresql":
        # D9: no two active bookings for the same assigned staff member overlap.
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            """
            ALTER TABLE bookings ADD CONSTRAINT bookings_staff_no_overlap
            EXCLUDE USING gist (staff_id WITH =, tstzrange(starts_at, ends_at) WITH &&)
            WHERE (staff_id IS NOT NULL AND status IN ('pending_payment', 'confirmed', 'in_progress'))
            """
        )

    op.create_table(
        "booking_services",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", UUID(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_amount", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_booking_services_booking_id", "booking_services", ["booking_id"])

    op.create_table(
        "booking_events",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("actor_id", UUID(), nullable=True),
        sa.Column("reason", sa.String(100), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_booking_events_booking_id", "booking_events", ["booking_id"])


def downgrade() -> None:
    op.drop_table("booking_events")
    op.drop_table("booking_services")
    op.drop_table("bookings")  # drops its indexes and the exclusion constraint with it
