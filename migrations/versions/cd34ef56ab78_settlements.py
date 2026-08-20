"""settlements table — each pending->available release is a first-class row

Revision ID: cd34ef56ab78
Revises: bc23de45fa67
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "cd34ef56ab78"
down_revision = "bc23de45fa67"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=40), nullable=False),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("txn_count", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="sweep"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_settlements_public_id", "settlements", ["public_id"], unique=True)
    op.create_index("ix_settlements_merchant_id", "settlements", ["merchant_id"])
    op.create_index("ix_settlements_created_at", "settlements", ["created_at"])


def downgrade():
    op.drop_index("ix_settlements_created_at", table_name="settlements")
    op.drop_index("ix_settlements_merchant_id", table_name="settlements")
    op.drop_index("ix_settlements_public_id", table_name="settlements")
    op.drop_table("settlements")
