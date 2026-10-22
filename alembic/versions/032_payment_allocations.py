"""payment_allocations: one checkout payment for a whole visit, split per booking (and tip per staff)

Revision ID: 032
Revises: 031
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("visit_id", UUID(), nullable=True))
        batch.create_foreign_key("fk_payments_visit_id", "visits", ["visit_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_payments_visit_created", "payments", ["visit_id", "created_at"])
    op.create_table(
        "payment_allocations",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("payment_id", UUID(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", UUID(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("tip_amount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_payment_allocations_payment_id", "payment_allocations", ["payment_id"])
    op.create_index("ix_payment_allocations_booking_id", "payment_allocations", ["booking_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_allocations_booking_id", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_payment_id", table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index("ix_payments_visit_created", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("fk_payments_visit_id", type_="foreignkey")
        batch.drop_column("visit_id")
