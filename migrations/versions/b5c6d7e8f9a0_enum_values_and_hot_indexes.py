"""channel enum gains VISA/CRYPTO on Postgres + hot-path indexes

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
Create Date: 2026-08-18

CRITICAL HALF: the Python Channel enum gained VISA and CRYPTO (used by the
Flutterwave-passthrough and ChangeNow crypto checkout), but no migration ever
ran ALTER TYPE ... ADD VALUE — the initial schema created the Postgres enum
with only MTN_MOMO/AIRTEL_MONEY/CARD, and the later migration that "added"
the channels only referenced the 5-value enum inside a NEW table's column
definition, which does not alter an existing type. On production Postgres the
FIRST crypto charge to settle would crash create_charge with an invalid enum
value — after the customer has already sent real crypto. Found by the
engineering audit's schema agent before any customer hit it.

INDEX HALF (hot query paths with no index):
- payment_links.transaction_id — queried on every successful charge by
  vending.maybe_dispense_on_success (filter_by(transaction_id=...)).
- webhook_deliveries (status, next_attempt_at) — the 30s beat sweep filters
  on both; only next_attempt_at was indexed.
- idempotency_keys.created_at — enables a future retention job to prune the
  unbounded table without a full scan.
"""
from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ADD VALUE cannot run inside the migration transaction on older PG;
        # the autocommit block handles that correctly on all versions.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'VISA'")
            op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'CRYPTO'")
    # SQLite stores enums as VARCHAR — nothing to do there.

    op.create_index("ix_payment_links_transaction_id", "payment_links", ["transaction_id"])
    op.create_index("ix_webhook_deliveries_status_next", "webhook_deliveries",
                    ["status", "next_attempt_at"])
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade():
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_index("ix_webhook_deliveries_status_next", table_name="webhook_deliveries")
    op.drop_index("ix_payment_links_transaction_id", table_name="payment_links")
    # Postgres enum values cannot be dropped — leaving VISA/CRYPTO in place is harmless.
