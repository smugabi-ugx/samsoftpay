"""KYC re-verification: merchant expiry + re-verify flag + director ID expiry

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-29

Stripe-standard re-verification. A merchant can be VERIFIED yet not currently
allowed to move live money:
  - merchants.kyc_expires_at   — verification lapses when a submitted ID expires
                                 (set at approval from the director's id_expiry);
                                 NULL = never expires (backward-compatible).
  - merchants.reverify_required — review can flag the account (abuse, limit
                                  breach, periodic re-KYC) to force re-verification
                                  WITHOUT discarding the KYC record.
  - merchants.reverify_reason  — human reason, shown to the merchant.
  - kyc_directors.id_expiry    — the printed expiry of the director's ID, which
                                 drives merchants.kyc_expires_at.

Merchant.kyc_is_current() is the single money gate; all existing rows default to
no expiry and reverify_required=False, so this is fully backward-compatible.
"""
from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("merchants", sa.Column("kyc_expires_at", sa.DateTime(), nullable=True))
    op.add_column("merchants", sa.Column(
        "reverify_required", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("merchants", sa.Column("reverify_reason", sa.String(500), nullable=True))
    op.add_column("kyc_directors", sa.Column("id_expiry", sa.String(20), nullable=True))
    # Drop the server_default now that existing rows are populated — the model
    # owns the default going forward.
    with op.batch_alter_table("merchants") as batch:
        batch.alter_column("reverify_required", server_default=None)


def downgrade():
    op.drop_column("kyc_directors", "id_expiry")
    op.drop_column("merchants", "reverify_reason")
    op.drop_column("merchants", "reverify_required")
    op.drop_column("merchants", "kyc_expires_at")
