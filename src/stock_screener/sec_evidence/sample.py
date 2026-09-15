"""Choosing the securities a controlled harvest reads, reproducibly.

A sample that changes between runs cannot be used to measure anything, so this
one is a pure function of the record: securities are sorted, bucketed, and taken
in order. No randomness, no clock, no set iteration order. Given the same
database it returns the same hundred securities on any machine.

**The buckets exist so the batch is not flattering.** Fetching a hundred large
live registrants would prove the pipeline works on the easiest input it will ever
see. The quotas deliberately include delisted securities with no CIK — the ones
Phase 9K.4 could not resolve — and symbols that changed hands, because those are
where a harvest actually breaks.

## Two populations, deliberately separate

`controlled_sample` is the 100-security validation batch that phases 9K.13-9K.18
measured. It is frozen: the same hundred securities in the same order, because
every figure recorded against it would otherwise stop meaning anything.

`production_universe` is the whole record — every live company and every delisted
security, in one deterministic order — and it is what a real harvest paginates
through. They are separate functions on purpose. Overloading the validation
sample with a bigger limit was the Phase 9K.18 defect: `--limit 250` silently
returned 100, because the quotas cap it there, and the run looked successful
while processing 60% fewer securities than asked for.

**And the batch is strided, not taken from the front.** Sorting and taking the
first twenty-five gave twenty-five tickers beginning `AC`: reproducible, and a
sample of the letter A rather than of the market. Each bucket is therefore walked
at an even stride across its whole pool, which is just as deterministic and
actually spans the register — `BBBY` and `SGEN` are reachable, and a run is not
measuring one corner of the alphabet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from data_access.models import Company, HistoricalSecurity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)

#: How many of each kind a default batch takes, and in which order they are
#: filled. The shares are deliberate: half the batch is delisted, because a
#: survivorship-aware corpus is exactly the part a live-only sample cannot test.
DEFAULT_QUOTAS: tuple[tuple[str, int], ...] = (
    ("LIVE_SHORT_TICKER", 15),
    ("LIVE_LONG_TICKER", 15),
    ("LIVE_REMAINDER", 20),
    ("DELISTED_WITH_CIK", 25),
    ("DELISTED_WITHOUT_CIK", 25),
)

#: A ticker at or below this length counts as short. Four is the boundary
#: between the classic exchange symbol and the five-letter Nasdaq forms that
#: carry class and status suffixes.
SHORT_TICKER = 4


#: Bucket labels for the production universe. The quota buckets describe how a
#: validation batch was *composed*; the production universe is not composed at
#: all, so it says only what kind of record a security is.
LIVE = "LIVE"
DELISTED = "DELISTED"


@dataclass(frozen=True)
class Selection:
    """One security a batch will read.

    Attributes:
        bucket: Which quota it filled, for the audit trail.
        symbol: Its ticker.
        name: The issuer name.
        cik: The filer number, or None where the free record establishes none.
        company_id: The live company's id, when it is one.
        historical_security_id: The delisted security's id, when it is one.
    """

    bucket: str
    symbol: str
    name: str
    cik: int | None
    company_id: int | None = None
    historical_security_id: int | None = None


def controlled_sample(
    session: Session,
    *,
    quotas: Sequence[tuple[str, int]] = DEFAULT_QUOTAS,
) -> tuple[Selection, ...]:
    """Return the deterministic controlled batch.

    Args:
        session: An open session.
        quotas: `(bucket, count)` pairs, filled in order.

    Returns:
        The selected securities, bucket by bucket. Shorter than the quotas ask
        for when the record holds fewer — a quota is a ceiling, never padding.
    """
    live = sorted(
        session.execute(select(Company)).scalars(),
        key=lambda c: (c.ticker or "").upper(),
    )
    delisted = sorted(
        session.execute(select(HistoricalSecurity)).scalars(),
        key=lambda h: ((h.primary_ticker or "").upper(), h.id),
    )

    pools: dict[str, list[Selection]] = {
        "LIVE_SHORT_TICKER": [
            _from_company(c, "LIVE_SHORT_TICKER")
            for c in live
            if len(c.ticker or "") <= SHORT_TICKER
        ],
        "LIVE_LONG_TICKER": [
            _from_company(c, "LIVE_LONG_TICKER") for c in live if len(c.ticker or "") > SHORT_TICKER
        ],
        "LIVE_REMAINDER": [_from_company(c, "LIVE_REMAINDER") for c in live],
        "DELISTED_WITH_CIK": [
            _from_security(h, "DELISTED_WITH_CIK") for h in delisted if h.cik is not None
        ],
        "DELISTED_WITHOUT_CIK": [
            _from_security(h, "DELISTED_WITHOUT_CIK") for h in delisted if h.cik is None
        ],
    }

    chosen: list[Selection] = []
    taken: set[tuple[str, int | None, int | None]] = set()
    for bucket, count in quotas:
        filled = 0
        for candidate in _strided(pools.get(bucket, []), count):
            if filled >= count:
                break
            key = (candidate.symbol, candidate.company_id, candidate.historical_security_id)
            if key in taken:
                # Already taken by an earlier bucket. `LIVE_REMAINDER` overlaps
                # the two ticker-length buckets on purpose, so it tops the batch
                # up with whatever they did not already claim.
                continue
            taken.add(key)
            chosen.append(candidate)
            filled += 1

    log.info(
        "controlled sample",
        selected=len(chosen),
        buckets={b: sum(1 for s in chosen if s.bucket == b) for b, _ in quotas},
    )
    return tuple(chosen)


def production_universe(session: Session) -> tuple[Selection, ...]:
    """Every security in the record, in one stable order.

    **The order is `(symbol, kind, id)`.** Symbol alone will not do: 138 symbols
    appear in both tables — the same business as a live row and as a delisted
    one, the `STRR` shape — so sorting on it leaves those pairs free to swap
    between runs and silently move a security from one chunk to another. Kind
    and the table's own primary key make the order total, and a primary key does
    not change, so the order is stable across processes and machines.

    Nothing is filtered. A delisted security with no CIK and a class ticker
    carrying a dot both stay in the population and simply produce no request
    later. That keeps the denominator honest: a chunk of 250 is 250 securities,
    not 250 *requestable* securities, and coverage arithmetic still adds up.

    Args:
        session: An open session.

    Returns:
        The whole population, ordered.
    """
    rows: list[tuple[tuple[str, str, int], Selection]] = []
    for company in session.execute(select(Company)).scalars():
        selection = _from_company(company, LIVE)
        rows.append(((selection.symbol, LIVE, company.id), selection))
    for security in session.execute(select(HistoricalSecurity)).scalars():
        selection = _from_security(security, DELISTED)
        rows.append(((selection.symbol, DELISTED, security.id), selection))
    rows.sort(key=lambda row: row[0])
    return tuple(selection for _, selection in rows)


def select_universe(
    session: Session, *, production: bool, offset: int = 0, limit: int | None = None
) -> tuple[Selection, ...]:
    """Return one deterministic slice of the chosen population.

    Pagination is `universe[offset : offset + limit]` and nothing else. It does
    **not** consult the manifest: which securities belong to a chunk must depend
    on the record and the arguments alone, or the same command run twice would
    cover different ground and a harvest would stop being reproducible.

    Args:
        session: An open session.
        production: True for the whole record, False for the frozen 100-security
            validation batch.
        offset: How many securities to skip. Beyond the end yields nothing.
        limit: How many to take. None takes the remainder.

    Returns:
        The slice, in universe order.

    Raises:
        ValueError: If `offset` is negative.
    """
    if offset < 0:
        message = f"offset must not be negative, got {offset}"
        raise ValueError(message)
    population = production_universe(session) if production else controlled_sample(session)
    end = None if limit is None else offset + limit
    return population[offset:end]


def _strided(pool: Sequence[Selection], count: int) -> list[Selection]:
    """Walk a sorted pool at an even stride, then fall back to the remainder.

    Deterministic: index `round(i * len(pool) / count)` for each `i`, which is a
    function of the pool alone. The strided picks come first so a truncated batch
    still spans the pool; everything else follows in order, so a quota larger
    than the stride can supply is still filled rather than silently short.

    Args:
        pool: Candidates, already sorted.
        count: How many the quota wants.

    Returns:
        The pool reordered so the first `count` entries are spread across it.
    """
    if count <= 0 or not pool:
        return []
    if count >= len(pool):
        return list(pool)
    indices = sorted({min(len(pool) - 1, round(i * len(pool) / count)) for i in range(count)})
    spread = [pool[i] for i in indices]
    remainder = [item for index, item in enumerate(pool) if index not in set(indices)]
    return spread + remainder


def _from_company(company: Company, bucket: str) -> Selection:
    """Project a live company into a selection."""
    return Selection(
        bucket=bucket,
        symbol=(company.ticker or "").upper(),
        name=company.name or "",
        cik=None,
        company_id=company.id,
    )


def _from_security(security: HistoricalSecurity, bucket: str) -> Selection:
    """Project a delisted security into a selection."""
    return Selection(
        bucket=bucket,
        symbol=(security.primary_ticker or "").upper(),
        name=security.name or "",
        cik=security.cik,
        historical_security_id=security.id,
    )
