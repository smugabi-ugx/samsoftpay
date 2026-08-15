"""split test and live ledgers: accounts.is_test

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-08-14

Sandbox and live money shared one set of ledger accounts. `is_test` was only a
label on the transaction/payout row, so a charge or payout made with an sk_test_
key moved the SAME balance a merchant could withdraw. Verified in production: a
1,000 UGX test payout took KarlPOS's available balance from 202,350 to 200,600.

An account is now identified by (type, merchant, currency, MODE), so the two
ledgers cannot touch.

MIGRATION OF EXISTING DATA: every existing account becomes is_test=False (live).
That preserves current balances exactly and is the honest default — historically
all money, including sandbox money, was posted there. It does NOT retroactively
unpick past test transactions from live balances; use `flask reverse-payout` for
any specific stranded test payout that needs undoing.
"""
from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table rebuilds the table on SQLite (which cannot ALTER a
    # constraint) and issues plain ALTERs on PostgreSQL.
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        # Widen the uniqueness key to include the mode.
        batch.drop_constraint("uq_account", type_="unique")
        batch.create_unique_constraint(
            "uq_account", ["type", "merchant_id", "currency", "is_test"]
        )


def downgrade():
    # Collapsing back would merge two ledgers into one and could violate the
    # narrower constraint, so drop sandbox accounts first — they hold no real
    # money by definition.
    op.execute("DELETE FROM journal_entries WHERE account_id IN "
               "(SELECT id FROM accounts WHERE is_test = true)")
    op.execute("DELETE FROM accounts WHERE is_test = true")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_account", type_="unique")
        batch.create_unique_constraint(
            "uq_account", ["type", "merchant_id", "currency"]
        )
        batch.drop_column("is_test")
