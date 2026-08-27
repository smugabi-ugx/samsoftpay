"""bank settlement — withdrawal_requests.bank_reference

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-28

Bank settlements are operator-confirmed (no automated bank rail yet): the
operator records the bank-transfer reference when they release the funds.
"""
from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("withdrawal_requests") as batch:
        batch.add_column(sa.Column("bank_reference", sa.String(120), nullable=True))


def downgrade():
    with op.batch_alter_table("withdrawal_requests") as batch:
        batch.drop_column("bank_reference")
