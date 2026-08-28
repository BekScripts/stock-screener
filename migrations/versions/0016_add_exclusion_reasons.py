"""Record why an ineligible company was not scored.

`scoring_status` says a company has no score. It has never said why, because the
eligibility screen's reasons were computed on every scan, written to the CSV scan
report and then discarded. So the dashboard could only ever render `NOT_ELIGIBLE`
— the same three words for a company reporting in Taiwan dollars, an ETF, a
delisted shell and a stock that trades a hundred dollars a day.

Those are four different answers to "why is this row blank", and only the first
is something Phase 7 can fix. Keeping the reasons is what lets a screen say
"reports in TWD; fundamentals are not yet normalised to USD" instead of showing
a wall of dashes.

Nullable, and deliberately not backfilled. The reasons for a historical row were
never recorded and cannot be recovered — re-deriving them from today's data would
put today's verdict on an old date and misdate exactly the change the column
exists to make visible. Existing rows keep NULL, meaning "not recorded", and fill
in on the next scoring run.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable exclusion-reasons column."""
    op.add_column(
        "score_snapshots",
        sa.Column("exclusion_reasons", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    """Drop the exclusion-reasons column."""
    op.drop_column("score_snapshots", "exclusion_reasons")
