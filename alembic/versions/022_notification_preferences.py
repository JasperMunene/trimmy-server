"""notification_preferences (reminder settings)

Revision ID: 022
Revises: 021
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reminder_24h", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reminder_2h", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reminder_sms", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reminder_push", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
