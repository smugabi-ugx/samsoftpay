"""add instant_settlement flag to merchants

Revision ID: c3a2b1d4e5f6
Revises: b2f1a9c4d5e6
Create Date: 2026-06-18

When True, a merchant's succeeded charges skip the 24h settlement hold and land
directly in available balance (used for our own products like KarlPOS so the
deposit -> withdraw timing has no gap).
"""
from alembic import op
import sqlalchemy as sa


revision = "c3a2b1d4e5f6"
down_revision = "b2f1a9c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(
            sa.Column("instant_settlement", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("instant_settlement")
