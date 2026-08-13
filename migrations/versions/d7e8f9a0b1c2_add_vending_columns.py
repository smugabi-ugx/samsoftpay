"""add vending (XY) columns to merchants and payment_links

Revision ID: d7e8f9a0b1c2
Revises: c3a2b1d4e5f6
Create Date: 2026-08-13

merchants.vending_enabled
    Per-merchant switch for the vending connector. A succeeded charge only
    triggers a machine dispense when this is on, so the capability is opt-in
    and can be turned off instantly if a machine misbehaves.

payment_links.vending_*
    A vending order IS a payment link (same checkout, same QR, same rails) plus
    the machine context needed to dispense afterwards. vending_meta holds the
    JSON order ({machine, goods, pay_account}); vending_status tracks the
    dispense lifecycle separately from the payment status.
"""
from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c3a2b1d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(
            sa.Column("vending_enabled", sa.Boolean(), nullable=False,
                      server_default=sa.false())
        )

    with op.batch_alter_table("payment_links") as batch:
        batch.add_column(sa.Column("vending_meta", sa.Text(), nullable=True))
        batch.add_column(sa.Column("vending_status", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("vending_error", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("vending_dispensed_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("payment_links") as batch:
        batch.drop_column("vending_dispensed_at")
        batch.drop_column("vending_error")
        batch.drop_column("vending_status")
        batch.drop_column("vending_meta")

    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("vending_enabled")
