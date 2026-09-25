"""otp attempt counter and send log

Revision ID: 003
Revises: 002
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auth_tokens",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "otp_send_log",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_otp_send_log_email_purpose_created",
        "otp_send_log",
        ["email", "purpose", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_otp_send_log_email_purpose_created", table_name="otp_send_log")
    op.drop_table("otp_send_log")
    op.drop_column("auth_tokens", "attempts")
