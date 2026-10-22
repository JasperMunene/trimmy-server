"""notification settings per category and channel; one delivery row per notification and channel

Revision ID: 026
Revises: 025
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("notification_preferences") as batch:
        # {"bookings": {"email": false}, ...}: only what someone changed; defaults live in code.
        batch.add_column(sa.Column("channels", sa.JSON(), nullable=True))
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.create_unique_constraint("uq_notification_deliveries_channel", ["notification_id", "channel"])


def downgrade() -> None:
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.drop_constraint("uq_notification_deliveries_channel", type_="unique")
    with op.batch_alter_table("notification_preferences") as batch:
        batch.drop_column("channels")
