"""Store the pipeline commands started from outside the CLI.

The dashboard runs commands by spawning them, and a spawned process is invisible
the moment the request that started it returns. This table is what makes it
visible again: what was asked for, whether it is still going, and where its
output went.

It stores no results. Every command already persists what it produces, so a
finished job is read through the endpoint serving the thing it produced — the
row here only answers "is it running, and did it work".

`pid` is what makes a stale row recoverable. An API restart leaves `RUNNING`
behind with no process attached, and the pid is the only way to tell that from a
run still in progress.

No foreign key to `companies`. `target` holds a ticker for a per-company job,
but a job outlives the row it refers to and a run against a delisted company is
still a fact about what happened.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the jobs table and the indexes the dashboard reads it by."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=500), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_recent", "jobs", ["started_at"])


def downgrade() -> None:
    """Drop the jobs table and its indexes."""
    op.drop_index("ix_jobs_recent", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
