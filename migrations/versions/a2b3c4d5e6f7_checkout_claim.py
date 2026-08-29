"""public-checkout concurrency claim: payment_links.checkout_claimed_at

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-29

Set atomically just before a charge is initiated from the public checkout so a
double-tap / concurrent double-scan of a single-use link cannot book two real
charges (transaction_id is only attached after create_charge returns). Nullable;
a stale claim is reclaimable.
"""
from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_links", sa.Column("checkout_claimed_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("payment_links", "checkout_claimed_at")
