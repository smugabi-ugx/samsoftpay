"""platform_flags table — DB-backed operational switches (payout kill switch)

Revision ID: bc23de45fa67
Revises: ab12cd34ef56
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "bc23de45fa67"
down_revision = "ab12cd34ef56"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_flags",
        sa.Column("key", sa.String(length=60), primary_key=True),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
    )


def downgrade():
    op.drop_table("platform_flags")
