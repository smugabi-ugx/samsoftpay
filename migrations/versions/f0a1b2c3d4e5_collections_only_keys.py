"""collections-only (scoped) API keys for kiosks/devices

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-19

A full secret key on a public kiosk (decompiled/rooted) is a money-OUT
credential — it can drain the merchant via /v1/payouts/bulk. A collections-only
key can create charges/orders but is 403'd on payouts and refunds. Existing
keys are unaffected and keep full scope; these are generated on demand.
"""
from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(sa.Column("collections_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("collections_key_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("test_collections_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("test_collections_key_hash", sa.String(64), nullable=True))
    op.create_index("ix_merchants_collections_key_hash", "merchants",
                    ["collections_key_hash"], unique=True)
    op.create_index("ix_merchants_test_collections_key_hash", "merchants",
                    ["test_collections_key_hash"], unique=True)


def downgrade():
    op.drop_index("ix_merchants_test_collections_key_hash", table_name="merchants")
    op.drop_index("ix_merchants_collections_key_hash", table_name="merchants")
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("test_collections_key_hash")
        batch.drop_column("test_collections_key")
        batch.drop_column("collections_key_hash")
        batch.drop_column("collections_key")
