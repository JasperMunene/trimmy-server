"""staff_services: which services each staff member performs

Revision ID: 007
Revises: 006
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("performs_all_services", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "staff_services",
        sa.Column("staff_id", UUID(), sa.ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("service_id", UUID(), sa.ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_staff_services_service_id", "staff_services", ["service_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_services_service_id", table_name="staff_services")
    op.drop_table("staff_services")
    op.drop_column("staff", "performs_all_services")
