"""visits: several bookings (different staff) for one customer's visit, checked out together

Revision ID: 031
Revises: 030
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "visits",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_visits_business_created", "visits", ["business_id", "created_at"])
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("visit_id", UUID(), nullable=True))
        batch.create_foreign_key("fk_bookings_visit_id", "visits", ["visit_id"], ["id"], ondelete="SET NULL")
    # Most bookings have no visit: index only the ones that do.
    op.create_index(
        "ix_bookings_visit_id", "bookings", ["visit_id"],
        postgresql_where=sa.text("visit_id IS NOT NULL"), sqlite_where=sa.text("visit_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_visit_id", table_name="bookings")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_constraint("fk_bookings_visit_id", type_="foreignkey")
        batch.drop_column("visit_id")
    op.drop_index("ix_visits_business_created", table_name="visits")
    op.drop_table("visits")
