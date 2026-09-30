"""booking reminder tracking

Revision ID: 015
Revises: 014
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("reminder_24h_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("reminder_2h_sent_at", sa.DateTime(timezone=True), nullable=True))
    # The reminder scan filters confirmed bookings by start time.
    op.create_index("ix_bookings_status_starts", "bookings", ["status", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_bookings_status_starts", table_name="bookings")
    op.drop_column("bookings", "reminder_2h_sent_at")
    op.drop_column("bookings", "reminder_24h_sent_at")
