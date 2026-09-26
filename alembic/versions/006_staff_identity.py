"""staff identity: working days + commission on staff, invitation -> staff link

Revision ID: 006
Revises: 005
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.db_types import UUID

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("staff", sa.Column("working_days", sa.JSON(), nullable=True))
    op.add_column("staff", sa.Column("commission_percent", sa.Integer(), nullable=True))
    # Batch mode so SQLite (dev/tests) can add the foreign key; Postgres just ALTERs.
    with op.batch_alter_table("team_invitations") as batch:
        batch.add_column(sa.Column("staff_id", UUID(), nullable=True))
        batch.create_foreign_key(
            "fk_team_invitations_staff_id", "staff", ["staff_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_team_invitations_staff_id", ["staff_id"])

    # Link accepted members to the Staff row created when they accepted...
    op.execute(
        """
        UPDATE team_invitations SET staff_id = (
            SELECT s.id FROM staff s
            WHERE s.business_id = team_invitations.business_id AND s.user_id = team_invitations.user_id
            LIMIT 1
        )
        WHERE status = 'accepted' AND user_id IS NOT NULL
        """
    )
    # ...and copy their roster details across.
    op.execute(
        """
        UPDATE staff SET
            working_days = (SELECT t.working_days FROM team_invitations t WHERE t.staff_id = staff.id LIMIT 1),
            commission_percent = (SELECT t.commission_percent FROM team_invitations t WHERE t.staff_id = staff.id LIMIT 1)
        WHERE id IN (SELECT staff_id FROM team_invitations WHERE staff_id IS NOT NULL)
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("team_invitations") as batch:
        batch.drop_index("ix_team_invitations_staff_id")
        batch.drop_constraint("fk_team_invitations_staff_id", type_="foreignkey")
        batch.drop_column("staff_id")
    op.drop_column("staff", "commission_percent")
    op.drop_column("staff", "working_days")
