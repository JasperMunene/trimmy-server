"""no-show fee on bookings

Revision ID: 016
Revises: 015
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("no_show_fee", "no_show_fee_uncollected", "no_show_fee_percent")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("bookings", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("bookings", name)
