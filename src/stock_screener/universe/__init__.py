"""The monthly research universe — Phase 9K.7.

The layer a strategy reads instead of guessing. It turns the audited free
evidence into one verdict per security per month, and it is deliberately narrow:
monthly resolution, 2022-01 onward, NASDAQ and NYSE, common stock, with every
exclusion named.

`eligible_for_month` is the whole downstream contract. It returns `ELIGIBLE` rows
and nothing else — an `UNKNOWN` security stays unknown, a censored venue stays
censored, and there is no parameter that relaxes either.
"""

from stock_screener.universe.audit import (
    CENSORED_VENUE_SPELLINGS,
    MonthCount,
    UniverseAudit,
    build_universe_audit,
    eligible_for_month,
)
from stock_screener.universe.build import (
    ATTRIBUTED,
    BuildReport,
    build_universe,
)

__all__ = [
    "ATTRIBUTED",
    "CENSORED_VENUE_SPELLINGS",
    "BuildReport",
    "MonthCount",
    "UniverseAudit",
    "build_universe",
    "build_universe_audit",
    "eligible_for_month",
]
