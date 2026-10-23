"""Trust & safety: reports; reviews.hidden_*; businesses.suspended_*; payment_destinations.reviewed_*

Revision ID: 037
Revises: 036
Create Date: 2026-10-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("reporter_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
    )
    op.create_index("ix_reports_target", "reports", ["target_type", "target_id", "status"])
    op.create_index("ix_reports_status_created", "reports", ["status", "created_at"])
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("hidden_reason", sa.Text(), nullable=True))
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("suspended_reason", sa.Text(), nullable=True))
    with op.batch_alter_table("payment_destinations") as batch:
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reviewed_by", UUID(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payment_destinations") as batch:
        batch.drop_column("reviewed_by")
        batch.drop_column("reviewed_at")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("suspended_reason")
        batch.drop_column("suspended_at")
    with op.batch_alter_table("reviews") as batch:
        batch.drop_column("hidden_reason")
        batch.drop_column("hidden_at")
    op.drop_table("reports")
