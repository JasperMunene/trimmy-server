"""Front desk: no commission; staff can also have front desk access (one person, two modes)

- staff.front_desk_access / team_invitations.front_desk_access: may use the front desk
  (manage bookings, charge). role stays 'staff' (takes appointments) or 'front_desk' (desk only).
- team_invitations.commission_percent becomes nullable: null for desk-only members.
- Existing front desk who are bookable (given services) become staff with desk access, so they keep
  the commission they earn today; everyone else front desk loses the (never meaningful) percent.

Revision ID: 030
Revises: 029
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("staff") as batch:
        batch.add_column(sa.Column("front_desk_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("team_invitations") as batch:
        batch.add_column(sa.Column("front_desk_access", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.alter_column("commission_percent", existing_type=sa.Integer(), nullable=True, server_default=None)

    op.execute("UPDATE staff SET front_desk_access = true WHERE role = 'front_desk'")
    op.execute("UPDATE team_invitations SET front_desk_access = true WHERE role = 'front_desk'")

    # Bookable front desk (performs everything, or has mapped services): staff with desk access.
    bookable = (
        "role = 'front_desk' AND (performs_all_services "
        "OR EXISTS (SELECT 1 FROM staff_services ss WHERE ss.staff_id = staff.id))"
    )
    op.execute(
        "UPDATE team_invitations SET role = 'staff' WHERE staff_id IN (SELECT id FROM staff WHERE " + bookable + ")"
    )
    op.execute("UPDATE staff SET role = 'staff' WHERE " + bookable)

    # Desk only: no commission.
    op.execute("UPDATE staff SET commission_percent = NULL WHERE role = 'front_desk'")
    op.execute("UPDATE team_invitations SET commission_percent = NULL WHERE role = 'front_desk'")


def downgrade() -> None:
    op.execute("UPDATE team_invitations SET commission_percent = 40 WHERE commission_percent IS NULL")
    with op.batch_alter_table("team_invitations") as batch:
        batch.alter_column("commission_percent", existing_type=sa.Integer(), nullable=False, server_default="40")
        batch.drop_column("front_desk_access")
    with op.batch_alter_table("staff") as batch:
        batch.drop_column("front_desk_access")
