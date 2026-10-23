"""staff_time_off (S4)

Revision ID: 027
Revises: 026
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_time_off",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_staff_time_off_business_starts", "staff_time_off", ["business_id", "starts_at"])
    op.create_index("ix_staff_time_off_staff_starts", "staff_time_off", ["staff_id", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_staff_time_off_staff_starts", table_name="staff_time_off")
    op.drop_index("ix_staff_time_off_business_starts", table_name="staff_time_off")
    op.drop_table("staff_time_off")
