"""disputes table — the customer's public recourse door (never moves money)

Revision ID: de45fa67bc89
Revises: cd34ef56ab78
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "de45fa67bc89"
down_revision = "cd34ef56ab78"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "disputes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=40), nullable=False),
        sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=False),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("contact", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("resolution_note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_disputes_public_id", "disputes", ["public_id"], unique=True)
    op.create_index("ix_disputes_transaction_id", "disputes", ["transaction_id"])
    op.create_index("ix_disputes_merchant_id", "disputes", ["merchant_id"])
    op.create_index("ix_disputes_status", "disputes", ["status"])


def downgrade():
    op.drop_index("ix_disputes_status", table_name="disputes")
    op.drop_index("ix_disputes_merchant_id", table_name="disputes")
    op.drop_index("ix_disputes_transaction_id", table_name="disputes")
    op.drop_index("ix_disputes_public_id", table_name="disputes")
    op.drop_table("disputes")
