"""external reconciliation exceptions table

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-19

The third leg of reconciliation: matching our records against MTN's own
answer, so a lost callback (money in our float, invisible to the ledger) or a
we-succeeded-MTN-failed divergence becomes a visible, resolvable exception
instead of silent drift. This is the artifact a partner's compliance review
asks for first.
"""
from alembic import op
import sqlalchemy as sa


revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recon_exceptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rail_reference", sa.String(120), nullable=False),
        sa.Column("txn_public_id", sa.String(40), nullable=True),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=True),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="critical"),
        sa.Column("our_status", sa.String(30), nullable=True),
        sa.Column("mtn_status", sa.String(30), nullable=True),
        sa.Column("our_amount", sa.BigInteger(), nullable=True),
        sa.Column("mtn_amount", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("rail_reference", name="uq_recon_exception_ref"),
    )
    op.create_index("ix_recon_exceptions_rail_reference", "recon_exceptions", ["rail_reference"])
    op.create_index("ix_recon_exceptions_txn_public_id", "recon_exceptions", ["txn_public_id"])
    op.create_index("ix_recon_exceptions_merchant_id", "recon_exceptions", ["merchant_id"])
    op.create_index("ix_recon_exceptions_status", "recon_exceptions", ["status"])
    op.create_index("ix_recon_exceptions_created_at", "recon_exceptions", ["created_at"])


def downgrade():
    op.drop_table("recon_exceptions")
