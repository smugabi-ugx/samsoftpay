"""payouts.reference — echo the merchant's business reference on payouts

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-28

The single POST /v1/payouts accepted a `reference` but never stored or returned
it, though the API/OpenAPI and the Backbone payout contract promise it is
persisted, echoed in the response, and carried on payout.* webhooks. Add the
column and index; existing rows get NULL (they never had one).
"""
from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payouts") as batch:
        batch.add_column(sa.Column("reference", sa.String(120), nullable=True))
    op.create_index("ix_payouts_reference", "payouts", ["reference"])


def downgrade():
    op.drop_index("ix_payouts_reference", table_name="payouts")
    with op.batch_alter_table("payouts") as batch:
        batch.drop_column("reference")
