"""scheduled_payouts — recurring payroll disbursements (money-OUT autopilot)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-28

The money-OUT mirror of subscription billing. Inline sa.Enum(name='channel')
reuses the existing type (SQLAlchemy's ENUM._on_table_create runs with
checkfirst=True, so Postgres skips the duplicate CREATE TYPE and SQLite builds
it inline) — the same pattern proven by efbce9790912 / d4e5f6a7b8c9.
"""
from alembic import op
import sqlalchemy as sa


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scheduled_payouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="UGX"),
        sa.Column("channel",
                  sa.Enum("MTN_MOMO", "AIRTEL_MONEY", "CARD", "VISA", "CRYPTO", name="channel"),
                  nullable=False, server_default="MTN_MOMO"),
        sa.Column("interval", sa.String(20), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=False),
        sa.Column("max_per_recipient", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_scheduled_payout_amount_positive"),
    )
    op.create_index("ix_scheduled_payouts_public_id", "scheduled_payouts", ["public_id"], unique=True)
    op.create_index("ix_scheduled_payouts_merchant_id", "scheduled_payouts", ["merchant_id"])
    op.create_index("ix_scheduled_payouts_status", "scheduled_payouts", ["status"])
    op.create_index("ix_scheduled_payouts_next_run_at", "scheduled_payouts", ["next_run_at"])
    op.create_index("ix_scheduled_payouts_created_at", "scheduled_payouts", ["created_at"])
    op.create_index("ix_scheduled_payouts_status_next_run", "scheduled_payouts",
                    ["status", "next_run_at"])


def downgrade():
    for ix in ("ix_scheduled_payouts_status_next_run", "ix_scheduled_payouts_created_at",
               "ix_scheduled_payouts_next_run_at", "ix_scheduled_payouts_status",
               "ix_scheduled_payouts_merchant_id", "ix_scheduled_payouts_public_id"):
        op.drop_index(ix, table_name="scheduled_payouts")
    op.drop_table("scheduled_payouts")
