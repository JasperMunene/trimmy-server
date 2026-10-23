"""Admin foundation: users.platform_role / totp_* / disabled_*, and the audit_log table

Revision ID: 035
Revises: 034
Create Date: 2026-10-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("platform_role", sa.String(20), nullable=True))
        batch.add_column(sa.Column("totp_secret", sa.Text(), nullable=True))
        batch.add_column(sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("totp_last_step", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("disabled_reason", sa.Text(), nullable=True))
    op.create_index("ix_users_platform_role", "users", ["platform_role"])

    op.create_table(
        "audit_log",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("actor_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(30), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_index("ix_users_platform_role", table_name="users")
    with op.batch_alter_table("users") as batch:
        for column in ("disabled_reason", "disabled_at", "totp_last_step", "totp_enabled_at", "totp_secret", "platform_role"):
            batch.drop_column(column)
