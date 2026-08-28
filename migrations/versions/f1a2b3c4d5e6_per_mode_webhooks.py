"""per-mode webhook endpoint: merchants.webhook_url_test + webhook_secret_test

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-08-28

An OPTIONAL separate sandbox webhook endpoint + secret, so a merchant's test
(sk_test_) events never reach their production receiver. Both columns are
nullable — when webhook_url_test is unset, test events fall back to the single
webhook_url exactly as before, so this is backward-compatible.
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("merchants", sa.Column("webhook_url_test", sa.String(500), nullable=True))
    op.add_column("merchants", sa.Column("webhook_secret_test", sa.String(80), nullable=True))


def downgrade():
    op.drop_column("merchants", "webhook_secret_test")
    op.drop_column("merchants", "webhook_url_test")
