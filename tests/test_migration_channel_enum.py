"""Regression guard: no migration may re-CREATE the `channel` enum type.

The `channel` Postgres ENUM is created exactly ONCE (first migration
9ef3bb747c8b) and extended in place by b5c6d7e8f9a0. A later migration that
references it inside op.create_table WITHOUT `create_type=False` re-emits
`CREATE TYPE channel` on an incremental upgrade — which is what `flask db
upgrade` does on every Render deploy. Postgres then aborts with
"type 'channel' already exists", failing the preDeployCommand and therefore
EVERY deploy. (SQLite has no enum types, so tests using db.create_all never
saw it — this bug shipped invisibly and broke prod for hours.)

This test compiles the FULL migration chain to offline Postgres SQL and asserts
`CREATE TYPE channel` appears at most once. Run: python tests/test_migration_channel_enum.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    env = dict(os.environ)
    env["FLASK_APP"] = "run.py"
    env["MOMO_USE_REAL"] = "0"
    # Any postgresql URL — --sql is OFFLINE and never connects, but it makes
    # Alembic emit Postgres DDL (where the enum-type clash actually happens).
    env["DATABASE_URL"] = "postgresql://u:p@localhost/db"

    # Render from the parent of the scheduled_payouts migration to the head(s).
    # (An earlier DATA migration, d8e9f0a1b2c3, does a live SELECT that can't run
    # offline; this range starts after it and covers scheduled_payouts + anything
    # newer — exactly where a duplicate CREATE TYPE would be introduced.)
    out = subprocess.run(
        [sys.executable, "-m", "flask", "db", "upgrade", "c9d0e1f2a3b4:heads", "--sql"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    sql = (out.stdout or "") + (out.stderr or "")
    assert "CREATE TABLE scheduled_payouts" in sql, \
        f"offline SQL generation failed — chain did not render:\n{sql[-800:]}"

    n = sql.upper().count("CREATE TYPE CHANNEL")
    assert n <= 1, (
        f"`CREATE TYPE channel` emitted {n} times on Postgres — a migration is "
        f"re-creating an existing enum type and WILL fail every incremental "
        f"deploy. Use sa.Enum(...).with_variant(postgresql.ENUM(..., "
        f"create_type=False), 'postgresql') in the offending create_table."
    )
    print(f"[1] CREATE TYPE channel emitted {n} time(s) across the whole chain (<=1) — OK")

    # And the column is still correctly typed as the channel enum.
    assert "channel channel" in sql.lower().replace("  ", " "), \
        "scheduled_payouts.channel lost its enum type"
    print("[2] scheduled_payouts.channel still typed as the channel enum — OK")

    print("\nchannel-enum migration guard passed.")


if __name__ == "__main__":
    main()
