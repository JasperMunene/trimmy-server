"""staff_portfolio_photos: photos of a staff member's work, uploaded by them or the owner

Revision ID: 029
Revises: 028
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_portfolio_photos",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_staff_portfolio_photos_staff_id", "staff_portfolio_photos", ["staff_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_portfolio_photos_staff_id", table_name="staff_portfolio_photos")
    op.drop_table("staff_portfolio_photos")
