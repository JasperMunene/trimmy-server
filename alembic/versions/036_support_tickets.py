"""Support tickets: support_tickets, ticket_messages, support_macros

Revision ID: 036
Revises: 035
Create Date: 2026-10-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.db_types import UUID

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("number", sa.String(8), nullable=False, unique=True),
        sa.Column("requester_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requester_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("requester_phone", sa.Text(), nullable=True),
        sa.Column("requester_email", sa.Text(), nullable=True),
        sa.Column("business_id", UUID(), sa.ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("booking_id", UUID(), sa.ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False, server_default="in_app"),
        sa.Column("category", sa.String(20), nullable=False, server_default="other"),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),
        sa.Column("assignee_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_customer_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_support_tickets_status_last", "support_tickets", ["status", "last_message_at"])
    op.create_index("ix_support_tickets_assignee_status", "support_tickets", ["assignee_id", "status"])
    op.create_index("ix_support_tickets_requester", "support_tickets", ["requester_id", "last_message_at"])

    op.create_table(
        "ticket_messages",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("ticket_id", UUID(), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("author_id", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachment_url", sa.Text(), nullable=True),
        sa.Column("attachment_key", sa.Text(), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False, server_default="in_app"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_messages_ticket_created", "ticket_messages", ["ticket_id", "created_at"])

    op.create_table(
        "support_macros",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("support_macros")
    op.drop_table("ticket_messages")
    op.drop_table("support_tickets")
