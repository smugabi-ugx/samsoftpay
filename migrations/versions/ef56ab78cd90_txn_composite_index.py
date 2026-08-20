"""Composite indexes for the hot dashboard/API transaction queries

Revision ID: ef56ab78cd90
Revises: de45fa67bc89
Create Date: 2026-08-20

The hottest patterns — WHERE merchant_id=? ORDER BY id DESC LIMIT n (dashboard
home + activity cursor) and WHERE merchant_id=? ORDER BY created_at DESC (CSV /
statement) — could filter on the single-column merchant_id index but then had
to sort all of that merchant's rows. For a high-volume merchant (KarlPOS post
migration) that sort is the cost. Composite indexes let Postgres satisfy
filter+order+limit directly from the index.
"""
from alembic import op
import sqlalchemy as sa

revision = "ef56ab78cd90"
down_revision = "de45fa67bc89"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_txn_merchant_id_id", "transactions",
                    ["merchant_id", sa.text("id DESC")])
    op.create_index("ix_txn_merchant_created", "transactions",
                    ["merchant_id", sa.text("created_at DESC")])


def downgrade():
    op.drop_index("ix_txn_merchant_created", table_name="transactions")
    op.drop_index("ix_txn_merchant_id_id", table_name="transactions")
