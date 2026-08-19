"""merge the three parallel feature heads into one

Revision ID: ab12cd34ef56
Revises: c1d2e3f4a5b6, d4e5f6a7b8c9, f0a1b2c3d4e5
Create Date: 2026-08-19

Scoped keys (f0a1b2c3d4e5), subscription dunning (c1d2e3f4a5b6) and split
payments (d4e5f6a7b8c9) were developed in parallel, all descending from
e9f0a1b2c3d4. This empty merge revision gives Alembic a single head again so
`flask db upgrade` (Render's preDeployCommand) applies all three cleanly.
"""

revision = "ab12cd34ef56"
down_revision = ("c1d2e3f4a5b6", "d4e5f6a7b8c9", "f0a1b2c3d4e5")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
