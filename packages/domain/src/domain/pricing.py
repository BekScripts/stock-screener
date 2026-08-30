"""Recovering the price a security actually traded at on a past date.

Stored bars are `adjustment=all`: every close is expressed in today's share
basis and net of every distribution since. That is the right basis for a return
— it is what makes a 4-for-1 split stop looking like a 75% loss — and it is the
wrong basis for anything denominated in dollars per share. A market
capitalisation, a price floor, a per-share threshold: all of them need the price
as it stood, and none of them is in the database.

This module reverses the split half of that adjustment, using real ex-dates from
the corporate-action record. It never infers an action from a price movement.

## What it recovers, and what it does not

**Splits: exactly.** A close is multiplied by the product of every split factor
with an ex-date after the date asked about. AAPL's stored close of 120.98 on
2020-08-28 becomes 483.92 once the 4-for-1 of 2020-08-31 is undone.

**Dividends: not at all.** `adjustment=all` also removes distributions, and this
module leaves that removal in place, so a recovered price is low by the
cumulative distribution yield since the date. For AAPL over six years that is
about 3%; for a high-yield security over the same span it is larger. The
residual is one-directional and known, which is why it is stated here rather
than hidden: a recovered price is a *lower bound* on what traded.

Removing it properly needs each dividend's amount and the close it was measured
against, which the corporate-action record carries — or, more simply, a raw
price series, which the provider can serve and this project does not store.
Either is a deliberate follow-on, not something to approximate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.models import CorporateActionKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from domain.models import CorporateActionRecord

#: The kinds that change the share basis and therefore the price scale.
_SPLIT_KINDS = frozenset({CorporateActionKind.FORWARD_SPLIT, CorporateActionKind.REVERSE_SPLIT})


def cumulative_split_factor(actions: Iterable[CorporateActionRecord], as_of: date) -> float:
    """Return how many of today's shares one share on `as_of` became.

    Multiplies the factor of every split whose ex-date falls **after** `as_of`.
    A split on the date itself is already reflected in that session's price, so
    the boundary is strict — including it would double-count the very day the
    adjustment pivots on.

    Args:
        actions: The security's corporate actions. Kinds other than splits are
            ignored.
        as_of: The date being asked about.

    Returns:
        The cumulative factor, 1.0 when no split has happened since.
    """
    factor = 1.0
    for action in actions:
        if action.kind not in _SPLIT_KINDS:
            continue
        if action.split_factor is None or action.split_factor <= 0:
            continue
        if action.ex_date > as_of:
            factor *= action.split_factor
    return factor


def as_traded_price(
    adjusted_close: float | None,
    actions: Iterable[CorporateActionRecord],
    as_of: date,
) -> float | None:
    """Return the split-basis price for a date, from its adjusted close.

    Args:
        adjusted_close: The stored close, in today's share basis.
        actions: The security's corporate actions.
        as_of: The date the close belongs to.

    Returns:
        The price on that day's own share basis, or None without a close. Low by
        the distributions removed since — see the module docstring.
    """
    if adjusted_close is None:
        return None
    return adjusted_close * cumulative_split_factor(actions, as_of)


def market_cap_as_of(
    shares_outstanding: float | None,
    adjusted_close: float | None,
    actions: Sequence[CorporateActionRecord],
    share_count_date: date,
) -> float | None:
    """Return the market capitalisation on a past date.

    The two inputs must share a share basis, and that is the whole difficulty.
    A share count stated before a split is on the pre-split basis; the stored
    close is on today's. Multiplying them directly is wrong by exactly the split
    factor — for AAPL on 2020-08-28, a factor of four.

    **The date that anchors the correction is the share count's, not the
    valuation's**, and that distinction is the entire subtlety here. AAPL split
    on 2020-08-31 and did not file again until 2020-10-30, so for two months the
    newest available count was 4.28bn pre-split shares while the price had
    already quartered. Anchoring on the valuation date applies no factor at all
    across that window and values a two-trillion-dollar company at 557bn.
    Anchoring on the count's own date applies the factor to both months, which
    is what every market participant did at the time — 4.28bn times four is
    arithmetic, not a forecast, and no one waited for the 10-Q to do it.

    Stated as a formula, the two adjustments collapse into one: advancing the
    share count to the valuation date and returning the price to the count's
    basis multiply out to the same product, so only one factor is ever applied.

    Args:
        shares_outstanding: Shares as stated by a filing available on the
            valuation date. The caller is responsible for that availability;
            this function performs no selection and no I/O.
        adjusted_close: The stored close for the date being valued.
        actions: The security's corporate actions.
        share_count_date: The date `shares_outstanding` describes — a cover-page
            count's own "as of" instant, not the date the filing carrying it was
            submitted, and not the date being valued.

    Returns:
        The market capitalisation, or None when either input is missing. Low by
        the distributions removed since the date — a lower bound, not an
        estimate.
    """
    price = as_traded_price(adjusted_close, actions, share_count_date)
    if price is None or shares_outstanding is None or shares_outstanding <= 0:
        return None
    return shares_outstanding * price
