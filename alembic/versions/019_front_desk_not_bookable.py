"""front desk staff perform no services by default

Revision ID: 019
Revises: 018
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Front desk were created as "performs every service", which made them bookable by
    # customers. Only those still on that default change; explicit mappings are kept.
    op.execute("UPDATE staff SET performs_all_services = false WHERE role = 'front_desk' AND performs_all_services = true")


def downgrade() -> None:
    pass  # can't tell which ones the owner has changed since; leave them as they are
