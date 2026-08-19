"""subscription dunning: retry_count + next_retry_at

Revision ID: c1d2e3f4a5b6
Revises: e9f0a1b2c3d4
Create Date: 2026-08-19

A failed MoMo charge used to set a subscription to 'failed' permanently on the
first miss — in Uganda, where wallets top up irregularly, one bad-timing charge
churns the plan forever. Dunning retries a past_due subscription on a backoff
before giving up, recovering recurring revenue.

NOTE: this and the scoped-keys migration (f0a1b2c3d4e5) both descend from
e9f0a1b2c3d4. Whichever ships second may leave two Alembic heads — resolve with
`flask db merge heads` (or rebase this down_revision onto f0a1b2c3d4e5).
"""
from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("retry_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("next_retry_at", sa.DateTime(), nullable=True))
    op.create_index("ix_subscriptions_next_retry_at", "subscriptions", ["next_retry_at"])


def downgrade():
    op.drop_index("ix_subscriptions_next_retry_at", table_name="subscriptions")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("next_retry_at")
        batch.drop_column("retry_count")
