"""business timezone and booking settings

Revision ID: 008
Revises: 007
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("timezone", sa.Text(), "Africa/Nairobi"),
    ("slot_interval_minutes", sa.Integer(), "15"),
    ("buffer_minutes", sa.Integer(), "0"),
    ("min_lead_minutes", sa.Integer(), "30"),
    ("max_days_ahead", sa.Integer(), "30"),
)


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column("businesses", sa.Column(name, type_, nullable=False, server_default=default))


def downgrade() -> None:
    for name, _, _ in reversed(_COLUMNS):
        op.drop_column("businesses", name)
