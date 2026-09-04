"""anomaly_events — persisted fraud/abuse anomaly feed (admin-reviewable)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-29

The anomaly scans (payout drain, refund outliers, and the new charge-side
detection) already PAGE a human via send_alert. This table gives the anomalies
an in-app, reviewable home + the monitoring audit trail a bank's compliance
review asks for. One OPEN row per (kind, merchant_id, subject); resolving lets a
later recurrence open a fresh row. Fully additive — nothing else depends on it.

Chained AFTER the KYC re-verification migration (c4d5e6f7a8b9) so the history
stays linear when both merge to main.
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "anomaly_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("category", sa.String(20), nullable=False, server_default="charge"),
        sa.Column("severity", sa.String(20), nullable=False, server_default="warning"),
        sa.Column("merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=True),
        sa.Column("subject", sa.String(120), nullable=True),
        sa.Column("metric", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_anomaly_events_kind", "anomaly_events", ["kind"])
    op.create_index("ix_anomaly_events_category", "anomaly_events", ["category"])
    op.create_index("ix_anomaly_events_merchant_id", "anomaly_events", ["merchant_id"])
    op.create_index("ix_anomaly_events_dedupe_key", "anomaly_events", ["dedupe_key"])
    op.create_index("ix_anomaly_events_status", "anomaly_events", ["status"])
    op.create_index("ix_anomaly_events_created_at", "anomaly_events", ["created_at"])


def downgrade():
    op.drop_index("ix_anomaly_events_created_at", table_name="anomaly_events")
    op.drop_index("ix_anomaly_events_status", table_name="anomaly_events")
    op.drop_index("ix_anomaly_events_dedupe_key", table_name="anomaly_events")
    op.drop_index("ix_anomaly_events_merchant_id", table_name="anomaly_events")
    op.drop_index("ix_anomaly_events_category", table_name="anomaly_events")
    op.drop_index("ix_anomaly_events_kind", table_name="anomaly_events")
    op.drop_table("anomaly_events")
