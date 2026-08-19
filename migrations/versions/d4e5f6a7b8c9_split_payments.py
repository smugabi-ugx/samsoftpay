"""split payments / subaccounts

Revision ID: d4e5f6a7b8c9
Revises: e9f0a1b2c3d4
Create Date: 2026-08-19

A platform (reseller wallet, KarlPOS, TK) takes ONE customer payment and settles
portions to MULTIPLE sub-merchants. Subaccounts are managed Merchant rows (they
inherit the whole ledger/settlement/payout machinery); shares are recorded as
split_allocations and settle per-share after the charge's hold.

NOTE: this migration, the scoped-keys one (f0a1b2c3d4e5) and the dunning one
(c1d2e3f4a5b6) all descend from e9f0a1b2c3d4. Whichever ships after another
needs `flask db merge heads` (or a rebased down_revision) — resolve at merge.
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    # Column first, then a NAMED FK — an inline unnamed ForeignKey inside
    # batch_alter_table fails on SQLite ("Constraint must have a name").
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(sa.Column("parent_merchant_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("is_managed", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.create_foreign_key("fk_merchants_parent_merchant", "merchants",
                                 ["parent_merchant_id"], ["id"])
    op.create_index("ix_merchants_parent_merchant_id", "merchants", ["parent_merchant_id"])

    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("split_meta", sa.Text(), nullable=True))

    op.create_table(
        "subaccounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False),
        sa.Column("platform_merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("external_ref", sa.String(120), nullable=True),
        sa.Column("payout_phone", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_subaccount_public_id"),
        sa.UniqueConstraint("merchant_id", name="uq_subaccount_merchant"),
    )
    op.create_index("ix_subaccounts_platform_merchant_id", "subaccounts", ["platform_merchant_id"])

    op.create_table(
        "split_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=False),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("transaction_id", "merchant_id", name="uq_split_alloc"),
        sa.CheckConstraint("amount > 0", name="ck_split_alloc_amount_positive"),
    )
    op.create_index("ix_split_allocations_transaction_id", "split_allocations", ["transaction_id"])
    op.create_index("ix_split_allocations_merchant_id", "split_allocations", ["merchant_id"])
    op.create_index("ix_split_allocations_settled_at", "split_allocations", ["settled_at"])


def downgrade():
    op.drop_table("split_allocations")
    op.drop_table("subaccounts")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("split_meta")
    op.drop_index("ix_merchants_parent_merchant_id", table_name="merchants")
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("is_managed")
        batch.drop_column("parent_merchant_id")
