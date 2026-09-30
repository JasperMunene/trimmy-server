"""normalise team invitation emails (lowercase) and phones (E.164)

Revision ID: 021
Revises: 020
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.phone_utils import InvalidPhoneNumberError, normalise_phone

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, email, phone FROM team_invitations")).fetchall()
    for id_, email, phone in rows:
        new_email = (email or "").strip().lower() or None
        new_phone = (phone or "").strip() or None
        if new_phone:
            try:
                new_phone = normalise_phone(new_phone)
            except InvalidPhoneNumberError:
                pass
        if (new_email, new_phone) != (email, phone):
            conn.execute(
                sa.text("UPDATE team_invitations SET email = :e, phone = :p WHERE id = :id"),
                {"e": new_email, "p": new_phone, "id": id_},
            )


def downgrade() -> None:
    pass  # the original spellings aren't kept
