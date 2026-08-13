"""per-merchant XY credentials + vending machine registry

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-14

The XY credentials were global env vars, which allowed exactly one vending
operator on the whole platform. They belong to the merchant: XY issues a
key/secret/merchant-number per operator. The secret is stored encrypted because
the signing algorithm needs it back in plaintext.

vending_machines mirrors the operator's machines (one shbh owns many jqbh) so
orders can be validated and machines chosen by name.
"""
from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(sa.Column("xy_key", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("xy_secret_encrypted", sa.Text(), nullable=True))
        batch.add_column(sa.Column("xy_merchant_no", sa.String(length=60), nullable=True))
        batch.add_column(sa.Column("xy_base_url", sa.String(length=200), nullable=True))

    op.create_table(
        "vending_machines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"),
                  nullable=False, index=True),
        sa.Column("jqbh", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("machine_type", sa.String(length=40), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("latitude", sa.String(length=40), nullable=True),
        sa.Column("longitude", sa.String(length=40), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("merchant_id", "jqbh", name="uq_machine_per_merchant"),
    )


def downgrade():
    op.drop_table("vending_machines")
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("xy_base_url")
        batch.drop_column("xy_merchant_no")
        batch.drop_column("xy_secret_encrypted")
        batch.drop_column("xy_key")
