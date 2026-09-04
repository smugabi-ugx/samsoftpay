"""composite index for the settlement due-query

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-08-29

The hourly settlement sweep gathers (status=SUCCEEDED AND settled_at IS NULL AND
completed_at <= cutoff). Only `status` and `settled_at` were indexed individually,
so as the succeeded-txn table grows the planner can't satisfy the full predicate
from one index and the gather scan widens. A composite index over the three
predicate columns keeps the sweep cheap at volume.
"""
from alembic import op


revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_txn_settlement_due", "transactions",
                    ["status", "settled_at", "completed_at"])


def downgrade():
    op.drop_index("ix_txn_settlement_due", table_name="transactions")
