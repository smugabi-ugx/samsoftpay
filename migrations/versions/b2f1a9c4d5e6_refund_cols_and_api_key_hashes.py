"""add refund columns to transactions, api key hash columns to merchants, and a settlement index

Revision ID: b2f1a9c4d5e6
Revises: af632d0cd402
Create Date: 2026-06-14

Phase 1 commercial-readiness:
- transactions.refunded_at / refund_payout_id were added to the model when refunds
  shipped but never had a migration, so the refund endpoint would fail on a real DB.
- merchants.secret_key_hash / test_secret_key_hash back the lookup-by-hash auth so a
  database leak cannot expose usable API keys.
- composite index (status, completed_at) speeds the hourly settlement sweep at volume.
"""
from alembic import op
import sqlalchemy as sa


revision = "b2f1a9c4d5e6"
down_revision = "af632d0cd402"
branch_labels = None
depends_on = None


def upgrade():
    # --- transactions: refund columns (model had them, migration was missing) ---
    #     + settled_at so the settlement sweep settles each txn exactly once.
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("refunded_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("refund_payout_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("settled_at", sa.DateTime(), nullable=True))
    op.create_index("ix_transactions_settled_at", "transactions", ["settled_at"])

    # --- merchants: API key hash columns (nullable; backfilled separately) ---
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(sa.Column("secret_key_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("test_secret_key_hash", sa.String(length=64), nullable=True))

    op.create_index("ix_merchants_secret_key_hash", "merchants", ["secret_key_hash"], unique=True)
    op.create_index("ix_merchants_test_secret_key_hash", "merchants", ["test_secret_key_hash"], unique=True)

    # --- settlement sweep performance ---
    op.create_index(
        "ix_transactions_status_completed_at",
        "transactions",
        ["status", "completed_at"],
    )


def downgrade():
    op.drop_index("ix_transactions_status_completed_at", table_name="transactions")
    op.drop_index("ix_transactions_settled_at", table_name="transactions")
    op.drop_index("ix_merchants_test_secret_key_hash", table_name="merchants")
    op.drop_index("ix_merchants_secret_key_hash", table_name="merchants")
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("test_secret_key_hash")
        batch.drop_column("secret_key_hash")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("settled_at")
        batch.drop_column("refund_payout_id")
        batch.drop_column("refunded_at")
