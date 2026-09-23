"""business tables

Revision ID: 002
Revises: 001
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "businesses",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "owner_id",
            UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("amenities", sa.JSON(), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=True),
        sa.Column("location", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("onboarding_step", sa.String(length=50), nullable=True),
        sa.Column("team_mode", sa.String(length=20), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_businesses_owner_id", "businesses", ["owner_id"])
    op.create_index("ix_businesses_phone", "businesses", ["phone"])
    op.create_index("ix_businesses_status", "businesses", ["status"])
    op.create_index("ix_businesses_created_at", "businesses", ["created_at"])
    op.create_index("idx_businesses_owner_status", "businesses", ["owner_id", "status"])

    op.create_table(
        "business_photos",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "is_cover",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_business_photos_business_id", "business_photos", ["business_id"])

    op.create_table(
        "working_hours",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weekly_hours", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_working_hours_business_id",
        "working_hours",
        ["business_id"],
        unique=True,
    )

    op.create_table(
        "service_categories",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_service_categories_business_id", "service_categories", ["business_id"])

    op.create_table(
        "services",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            UUID(),
            sa.ForeignKey("service_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_amount", sa.Integer(), nullable=False),
        sa.Column(
            "price_currency",
            sa.String(length=3),
            nullable=False,
            server_default="KES",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_services_business_id", "services", ["business_id"])
    op.create_index("ix_services_category_id", "services", ["category_id"])

    op.create_table(
        "business_policies",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "deposit_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("deposit_type", sa.String(length=20), nullable=True),
        sa.Column("deposit_amount", sa.Integer(), nullable=True),
        sa.Column("deposit_currency", sa.String(length=3), nullable=True),
        sa.Column("deposit_applies_to", sa.String(length=30), nullable=True),
        sa.Column(
            "cancellation_free_hours",
            sa.Integer(),
            nullable=False,
            server_default="24",
        ),
        sa.Column(
            "cancellation_late_fee_percent",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
        sa.Column(
            "no_show_fee_percent",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_business_policies_business_id",
        "business_policies",
        ["business_id"],
        unique=True,
    )

    op.create_table(
        "payment_destinations",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("till_number", sa.Text(), nullable=True),
        sa.Column("paybill_number", sa.Text(), nullable=True),
        sa.Column("paybill_account_number", sa.Text(), nullable=True),
        sa.Column("bank_name", sa.Text(), nullable=True),
        sa.Column("bank_shortcode", sa.Text(), nullable=True),
        sa.Column("bank_account_number", sa.Text(), nullable=True),
        sa.Column(
            "verification_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_payment_destinations_business_id",
        "payment_destinations",
        ["business_id"],
        unique=True,
    )

    op.create_table(
        "team_invitations",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("invited_by", UUID(), nullable=False),
        sa.Column("user_id", UUID(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_team_invitations_business_id", "team_invitations", ["business_id"])

    op.create_table(
        "staff",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column(
            "rating",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("specialties", sa.JSON(), nullable=True),
        sa.Column("portfolio", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_staff_business_id", "staff", ["business_id"])

    op.create_table(
        "reviews",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "business_id",
            UUID(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "staff_id",
            UUID(),
            sa.ForeignKey("staff.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_reviews_business_id", "reviews", ["business_id"])
    op.create_index("ix_reviews_staff_id", "reviews", ["staff_id"])


def downgrade() -> None:
    op.drop_index("ix_reviews_staff_id", table_name="reviews")
    op.drop_index("ix_reviews_business_id", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("ix_staff_business_id", table_name="staff")
    op.drop_table("staff")
    op.drop_index("ix_team_invitations_business_id", table_name="team_invitations")
    op.drop_table("team_invitations")
    op.drop_index("ix_payment_destinations_business_id", table_name="payment_destinations")
    op.drop_table("payment_destinations")
    op.drop_index("ix_business_policies_business_id", table_name="business_policies")
    op.drop_table("business_policies")
    op.drop_index("ix_services_category_id", table_name="services")
    op.drop_index("ix_services_business_id", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_service_categories_business_id", table_name="service_categories")
    op.drop_table("service_categories")
    op.drop_index("ix_working_hours_business_id", table_name="working_hours")
    op.drop_table("working_hours")
    op.drop_index("ix_business_photos_business_id", table_name="business_photos")
    op.drop_table("business_photos")
    op.drop_index("idx_businesses_owner_status", table_name="businesses")
    op.drop_index("ix_businesses_created_at", table_name="businesses")
    op.drop_index("ix_businesses_status", table_name="businesses")
    op.drop_index("ix_businesses_phone", table_name="businesses")
    op.drop_index("ix_businesses_owner_id", table_name="businesses")
    op.drop_table("businesses")
