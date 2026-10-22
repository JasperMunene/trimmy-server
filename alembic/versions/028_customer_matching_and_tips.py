"""users.phone_verified_at; tips at checkout (payments.tip_amount, bookings.tip_amount)

Revision ID: 028
Revises: 027
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        # Set when the phone is proven with a code; cleared when it's changed without one. Only a
        # proven phone links bookings a business makes for that number to the account.
        batch.add_column(sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    # Phone-first accounts proved their number at signup.
    op.execute("UPDATE users SET phone_verified_at = verified_at WHERE auth_type = 'phone' AND verified AND phone IS NOT NULL")

    with op.batch_alter_table("payments") as batch:
        # The part of `amount` that is a tip for the staff member (checkout charges only).
        batch.add_column(sa.Column("tip_amount", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("tip_amount", sa.Integer(), nullable=True))
    op.create_index("ix_bookings_customer_phone", "bookings", ["customer_phone"])


def downgrade() -> None:
    op.drop_index("ix_bookings_customer_phone", table_name="bookings")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("tip_amount")
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("tip_amount")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("phone_verified_at")
