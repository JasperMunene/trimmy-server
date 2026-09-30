"""reviews linked to their booking and author

Revision ID: 020
Revises: 019
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("booking_id", UUID(), nullable=True))
        batch.add_column(sa.Column("customer_id", UUID(), nullable=True))
        batch.create_foreign_key("fk_reviews_booking_id", "bookings", ["booking_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_reviews_customer_id", "users", ["customer_id"], ["id"], ondelete="SET NULL")
        batch.create_unique_constraint("uq_reviews_booking_id", ["booking_id"])


def downgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.drop_constraint("uq_reviews_booking_id", type_="unique")
        batch.drop_constraint("fk_reviews_customer_id", type_="foreignkey")
        batch.drop_constraint("fk_reviews_booking_id", type_="foreignkey")
        batch.drop_column("customer_id")
        batch.drop_column("booking_id")
