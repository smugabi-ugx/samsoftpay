"""machine integration standard: signing_profiles + merchant vendor

Revision ID: b8c9d0e1f2a3
Revises: a1b2c3d4e5f6
Create Date: 2026-08-27

Machine Integration Standard v1. Per-vendor signing profiles replace the
hardcoded XY constants in webhooks_xy.py / xy_vending.py, so a new machine
vendor is a config row + a passed conformance sample, not a code change.

XY keeps working with NO dependency on this migration (it resolves to a built-in
default profile). The seeded XY row below simply makes the legacy profile
visible/editable and reproduces the built-in exactly. Every existing merchant is
backfilled onto the 'xy' vendor via the column server_default.
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "signing_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendor", sa.String(40), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("non_signed_fields", sa.Text(), nullable=False,
                  server_default='["sign", "key", "timestamp"]'),
        sa.Column("field_aliases", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("sign_order", sa.String(20), nullable=False, server_default="alpha"),
        sa.Column("sign_order_swaps", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("replay_window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispense_path", sa.String(255), nullable=False,
                  server_default="/service-pay-third/third/pay/api/ApplyExportGoods"),
        sa.Column("dispense_body_style", sa.String(40), nullable=False,
                  server_default="xy_orderdto"),
        sa.Column("dispense_extra", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("is_legacy_shim", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_signing_profiles_vendor", "signing_profiles",
                    ["vendor"], unique=True)

    # Merchants pick their machine-vendor profile; default 'xy' backfills all rows.
    with op.batch_alter_table("merchants") as batch:
        batch.add_column(sa.Column("signing_profile_vendor", sa.String(40),
                                   nullable=False, server_default="xy"))

    # Seed the XY legacy profile — identical to the built-in XY_PROFILE.
    from datetime import datetime, timezone
    op.bulk_insert(
        sa.table(
            "signing_profiles",
            sa.column("vendor", sa.String),
            sa.column("display_name", sa.String),
            sa.column("non_signed_fields", sa.Text),
            sa.column("field_aliases", sa.Text),
            sa.column("sign_order", sa.String),
            sa.column("sign_order_swaps", sa.Text),
            sa.column("replay_window_seconds", sa.Integer),
            sa.column("dispense_path", sa.String),
            sa.column("dispense_body_style", sa.String),
            sa.column("dispense_extra", sa.Text),
            sa.column("is_legacy_shim", sa.Boolean),
            sa.column("created_at", sa.DateTime),
        ),
        [{
            "vendor": "xy",
            "display_name": "XY Vending (legacy compatibility)",
            "non_signed_fields": json.dumps(["sign", "key", "timestamp", "splist"]),
            "field_aliases": json.dumps({"status": "state", "dsfshdh": "dsfshbh"}),
            "sign_order": "alpha_swap",
            "sign_order_swaps": json.dumps([["tkje", "tksj"]]),
            "replay_window_seconds": 0,
            "dispense_path": "/service-pay-third/third/pay/api/ApplyExportGoods",
            "dispense_body_style": "xy_orderdto",
            "dispense_extra": json.dumps({"consumeType": "hiTrade"}),
            "is_legacy_shim": True,
            "created_at": datetime.now(timezone.utc),
        }],
    )


def downgrade():
    with op.batch_alter_table("merchants") as batch:
        batch.drop_column("signing_profile_vendor")
    op.drop_index("ix_signing_profiles_vendor", table_name="signing_profiles")
    op.drop_table("signing_profiles")
