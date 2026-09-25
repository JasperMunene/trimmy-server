"""photo storage keys, deposit percent/service ids, team member fields

Revision ID: 005
Revises: 004
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("business_photos", sa.Column("storage_key", sa.Text(), nullable=True))

    op.add_column("business_policies", sa.Column("deposit_percent", sa.Integer(), nullable=True))
    op.add_column("business_policies", sa.Column("deposit_service_ids", sa.JSON(), nullable=True))
    # Percent deposits used to keep the percentage in deposit_amount.
    op.execute(
        "UPDATE business_policies SET deposit_percent = deposit_amount, deposit_amount = NULL "
        "WHERE deposit_type = 'percent'"
    )

    op.add_column("team_invitations", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "team_invitations",
        sa.Column("commission_percent", sa.Integer(), nullable=False, server_default="40"),
    )
    op.add_column("team_invitations", sa.Column("working_days", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_invitations", "working_days")
    op.drop_column("team_invitations", "commission_percent")
    op.drop_column("team_invitations", "name")

    op.execute(
        "UPDATE business_policies SET deposit_amount = deposit_percent "
        "WHERE deposit_type = 'percent'"
    )
    op.drop_column("business_policies", "deposit_service_ids")
    op.drop_column("business_policies", "deposit_percent")

    op.drop_column("business_photos", "storage_key")
