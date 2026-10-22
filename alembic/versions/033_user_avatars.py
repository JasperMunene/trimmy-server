"""users.avatar_url / avatar_key / google_picture_url: profile pictures

Revision ID: 033
Revises: 032
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("avatar_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("avatar_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("google_picture_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("google_picture_url")
        batch.drop_column("avatar_key")
        batch.drop_column("avatar_url")
