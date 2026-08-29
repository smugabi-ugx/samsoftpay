"""channel enum gains WALLET (on-us Samsoftpay balance transfers)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-29

Wallet v1 records an on-us transfer as a Transaction with channel=WALLET so it
shows in the payee's balance/dashboard/webhooks like any other collection. The
Postgres `channel` enum must learn the new value; SQLite stores enums as VARCHAR
so it needs nothing. Same ALTER-TYPE-ADD-VALUE pattern (in an autocommit block,
which older PG requires) used when VISA/CRYPTO were added.

SAEnum stores the enum member NAME, so the Postgres value is 'WALLET' (matching
how 'VISA'/'CRYPTO' were added), not the Python value 'samsoftpay_wallet'.
"""
from alembic import op


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'WALLET'")
    # SQLite stores enums as VARCHAR — nothing to do.


def downgrade():
    # Postgres cannot DROP a value from an enum without recreating the type;
    # leaving the (now-unused) value is harmless and standard for enum additions.
    pass
