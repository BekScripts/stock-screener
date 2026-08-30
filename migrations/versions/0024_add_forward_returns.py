"""Record what a security did after a point-in-time observation date.

A snapshot says what was knowable on a date. Nothing in this schema says what
happened next, so no study of whether the score predicts anything can be run
against it — the outcome half is simply absent.

This revision adds it, at five horizons: 1, 5, 20, 60 and 120 sessions. The
entry is the open of the session *after* the observation, because a snapshot
computable at a close cannot be filled at that close, and the horizon is counted
on the market's calendar rather than the security's, because a security that
stops trading for a month did not reach its horizon on time.

Three of the four statuses carry no number, and that is the point of storing
them. `TRUNCATED` is a horizon that has not elapsed yet and will realise later.
`INTERRUPTED` is a security that stopped trading inside it — an acquisition, a
bankruptcy, a move off-exchange — where a price series cannot say which, so
assuming -100% would libel every company bought for cash and assuming 0% would
erase every failure. `NO_ENTRY` is no first price at all. Dropping any of them
would rebuild the survivorship bias the point-in-time record exists to remove.

Append-only and versionless: a realised horizon is a measurement, not a policy,
and re-running one reproduces it exactly. The unique constraint refuses a second
row for a horizon already measured rather than replacing the first.

Additive. One new table, no existing table touched, no `batch_alter_table`.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the forward-return table."""
    op.create_table(
        "forward_returns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("total_return", sa.Float(), nullable=True),
        sa.Column("last_seen_date", sa.Date(), nullable=True),
        sa.Column("last_seen_price", sa.Float(), nullable=True),
        sa.Column(
            "measured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "observation_date", "horizon_sessions", name="uq_forward_return"
        ),
    )
    op.create_index(
        "ix_forward_returns_date",
        "forward_returns",
        ["observation_date", "horizon_sessions"],
        unique=False,
    )


def downgrade() -> None:
    """Drop it, index first."""
    op.drop_index("ix_forward_returns_date", table_name="forward_returns")
    op.drop_table("forward_returns")
