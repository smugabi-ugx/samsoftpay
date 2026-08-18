"""per-merchant webhook signing secret + backfill

Revision ID: d8e9f0a1b2c3
Revises: c6d7e8f9a0b1
Create Date: 2026-08-18

Outbound merchant webhooks were signed with the server-global
WEBHOOK_SIGNING_SECRET — a secret merchants could never see (so the
documented verification was impossible) and must never see (the same secret
authenticates inbound rail callbacks that mark money succeeded). Each
merchant now gets their own whsec_ secret; the global one becomes
inbound-only. Existing merchants are backfilled so signing can switch over
in one deploy with no unsigned window.
"""
import secrets

from alembic import op
import sqlalchemy as sa


revision = "d8e9f0a1b2c3"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("merchants", sa.Column("webhook_secret", sa.String(80), nullable=True))
    # Direct-dispense consumption claim: one SUCCEEDED charge used to
    # authorize UNLIMITED dispenses through POST /v1/vending/dispense (it
    # bypasses the order flow's _claim gate) — guardrail 11's "once" only
    # held inside the order flow. NULL = not yet consumed.
    op.add_column("transactions", sa.Column("vending_consumed_at", sa.DateTime(), nullable=True))
    # Backfill: one distinct secret per existing merchant.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM merchants WHERE webhook_secret IS NULL")).fetchall()
    for (mid,) in rows:
        bind.execute(
            sa.text("UPDATE merchants SET webhook_secret = :s WHERE id = :i"),
            {"s": "whsec_" + secrets.token_urlsafe(32), "i": mid},
        )


def downgrade():
    op.drop_column("transactions", "vending_consumed_at")
    op.drop_column("merchants", "webhook_secret")
