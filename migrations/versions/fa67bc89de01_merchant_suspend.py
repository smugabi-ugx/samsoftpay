"""Merchant suspend audit columns (admin console)

Revision ID: fa67bc89de01
Revises: ef56ab78cd90
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "fa67bc89de01"
down_revision = "ef56ab78cd90"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("merchants") as b:
        b.add_column(sa.Column("suspended_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("suspended_by", sa.String(length=120), nullable=True))
        b.add_column(sa.Column("suspend_reason", sa.String(length=500), nullable=True))


def downgrade():
    with op.batch_alter_table("merchants") as b:
        b.drop_column("suspend_reason")
        b.drop_column("suspended_by")
        b.drop_column("suspended_at")
