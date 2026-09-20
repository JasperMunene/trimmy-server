"""initial auth tables

Revision ID: 001
Revises:
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("email", sa.Text(), unique=True, nullable=True, index=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), unique=True, nullable=True, index=True),
        sa.Column("verified", sa.Boolean(), default=False, nullable=False, index=True),
        sa.Column("auth_type", sa.String(length=20), nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            index=True,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_users_email_verified", "users", ["email", "verified"])
    op.create_index("idx_users_auth_type_verified", "users", ["auth_type", "verified"])

    op.create_table(
        "auth_tokens",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "user_id",
            UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False, index=True),
        sa.Column(
            "purpose", sa.String(length=50), nullable=False, server_default="verification"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_auth_tokens_user_purpose", "auth_tokens", ["user_id", "purpose"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "user_id",
            UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column("revoked", sa.Boolean(), default=False, nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("auth_tokens")
    op.drop_table("users")
