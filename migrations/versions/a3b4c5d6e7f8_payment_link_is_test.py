"""payment_links.is_test — a link now remembers which key created it

Revision ID: a3b4c5d6e7f8
Revises: f9a0b1c2d3e4
Create Date: 2026-08-17

A payment link created with an sk_test_ key had no way to say so once it
existed: the public checkout page (GET /pay/<id>, no auth) and its submit
handler had no `is_test` to read, so checkout_submit hardcoded g.api_mode =
"live" for every link regardless of which key made it. Two consequences:

1. A test-mode purchase posted to the LIVE ledger, not the sandbox one — the
   exact class of bug guardrail 12 (f9a0b1c2d3e4) fixed for Transaction/Payout/
   Account, just missed here because PaymentLink was never given the column.
2. The simulated-rail guard (orchestrator._simulated_rail_forbidden, added in
   the same PR that filtered Airtel/Card off checkout pages) had no way to
   exempt a test-mode link either, so MTN's mock rail — the ENTIRE point of
   testing against a sandbox — was blocked identically to a real live charge.

MIGRATION OF EXISTING DATA: every existing link becomes is_test=False, the
same honest default guardrail 12 used — it does not retroactively reclassify
past links, only new ones created after this migration are correctly tagged.
"""
from alembic import op
import sqlalchemy as sa


revision = "a3b4c5d6e7f8"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("payment_links") as batch:
        batch.add_column(
            sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade():
    with op.batch_alter_table("payment_links") as batch:
        batch.drop_column("is_test")
