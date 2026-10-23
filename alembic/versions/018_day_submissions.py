"""day_submissions (front desk end-of-day, F4)

Revision ID: 018
Revises: 017
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "day_submissions",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("submitted_by", UUID(), nullable=False),
        sa.Column("submitted_by_name", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("business_id", "day", name="uq_day_submissions_business_day"),
    )


def downgrade() -> None:
    op.drop_table("day_submissions")
