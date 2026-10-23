"""reviews.reply_text / replied_at / replied_by: the business's reply to a review

Revision ID: 034
Revises: 033
Create Date: 2026-10-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("reply_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("replied_by", UUID(), nullable=True))
    # Business reviews list newest first, and the "needs a reply" filter.
    op.create_index("ix_reviews_business_created", "reviews", ["business_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_reviews_business_created", table_name="reviews")
    with op.batch_alter_table("reviews") as batch:
        batch.drop_column("replied_by")
        batch.drop_column("replied_at")
        batch.drop_column("reply_text")
