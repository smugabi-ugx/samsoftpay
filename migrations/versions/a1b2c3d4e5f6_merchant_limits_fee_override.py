"""Per-merchant limits + fee override (admin console Phase 2)

Revision ID: a1b2c3d4e5f6
Revises: fa67bc89de01
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "fa67bc89de01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as b:
        b.add_column(sa.Column("max_charge_amount", sa.BigInteger(), nullable=True))
        b.add_column(sa.Column("max_payout_amount", sa.BigInteger(), nullable=True))
        b.add_column(sa.Column("fee_bps_override", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("merchants") as b:
        b.drop_column("fee_bps_override")
        b.drop_column("max_payout_amount")
        b.drop_column("max_charge_amount")
