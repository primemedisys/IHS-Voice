"""add preferred appointment slot fields

Revision ID: a7c3e91b5d24
Revises: 150dd0ccb51a
Create Date: 2026-11-22 11:40:00.000000

Adds the caller's preferred day/time (a request, never a booking) to
inbound_inquiries and callbacks. Statements are idempotent (IF NOT EXISTS) so they are safe to re-run.

Do NOT run the downgrade on production: it drops columns that hold data.
"""
from typing import Sequence, Union

from alembic import op # type: ignore


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91b5d24'
down_revision: Union[str, Sequence[str], None] = '150dd0ccb51a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE inbound_inquiries ADD COLUMN IF NOT EXISTS preferred_date DATE")
    op.execute("ALTER TABLE inbound_inquiries ADD COLUMN IF NOT EXISTS preferred_time VARCHAR(30)")
    op.execute("ALTER TABLE inbound_inquiries ADD COLUMN IF NOT EXISTS preferred_slot_text TEXT")
    op.execute("ALTER TABLE callbacks ADD COLUMN IF NOT EXISTS preferred_slot TEXT")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE callbacks DROP COLUMN IF EXISTS preferred_slot")
    op.execute("ALTER TABLE inbound_inquiries DROP COLUMN IF EXISTS preferred_slot_text")
    op.execute("ALTER TABLE inbound_inquiries DROP COLUMN IF EXISTS preferred_time")
    op.execute("ALTER TABLE inbound_inquiries DROP COLUMN IF EXISTS preferred_date")
