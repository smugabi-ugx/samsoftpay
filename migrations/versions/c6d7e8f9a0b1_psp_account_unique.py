"""partial unique index for PSP-level (merchant_id IS NULL) accounts

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-18

uq_account (type, merchant_id, currency, is_test) never constrained the
PSP-level accounts (rail_clearing / psp_revenue / psp_float / suspense)
because merchant_id is NULL there and NULLs are distinct in unique
constraints. get_or_create_account's SAVEPOINT race guard depends on an
IntegrityError that could therefore never fire for those accounts — two
concurrent first-charges could commit duplicate PSP accounts, after which
every .one_or_none() lookup raises MultipleResultsFound and every charge in
that ledger 500s until manual cleanup. A partial unique index closes it
(supported by both PostgreSQL and SQLite).
"""
from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "uq_account_psp",
        "accounts",
        ["type", "currency", "is_test"],
        unique=True,
        sqlite_where=sa.text("merchant_id IS NULL"),
        postgresql_where=sa.text("merchant_id IS NULL"),
    )


def downgrade():
    op.drop_index("uq_account_psp", table_name="accounts")
