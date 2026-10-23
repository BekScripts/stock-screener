"""Backtest readiness: proving a past date can be studied without hindsight.

This package builds nothing a strategy needs and nothing a strategy is. It is
the harness that answers whether the point-in-time record is complete and honest
enough to be studied at all — the audit layer between the data and any future
research phase.

Five concerns, each a thin composition over already-tested parts:

- `reconstruct_universe` — which securities were in the investable set on a date,
  distinguishing what the record witnessed from what it merely assumes.
- `reconstruct_score` — the CompounderScore the frozen formula would have
  produced on a date, rebuilt from point-in-time inputs and changing no number.
- `backfill_signals` / `backfill_returns` — the same daily passes run over
  sessions that have already happened, idempotently and resumably, so the
  record can be built from stored prices instead of waited for.
- `date_coverage` — the same questions asked of the whole roster across many
  dates, reported as a funnel so an absent answer is attributed to a stage.
- `security_readiness` — the seven-question check that gathers universe,
  fundamentals, market cap, score, signals, forward outcomes and delisting fate
  behind one call, and names every gap it finds.
"""

from stock_screener.backtest.backfill import (
    BackfillReport,
    Cadence,
    DateResult,
    DateStatus,
    backfill_returns,
    backfill_signals,
    feature_coverage,
    market_sessions,
    sample,
)
from stock_screener.backtest.chain import (
    ChainAccounting,
    ChainStage,
    ChainVerdict,
    chain_accounting,
)
from stock_screener.backtest.coverage import (
    DateCoverage,
    ResearchWindow,
    date_coverage,
    research_window,
)
from stock_screener.backtest.harness import (
    DelistingFate,
    ForwardOutcome,
    SecurityReadiness,
    security_readiness,
)
from stock_screener.backtest.historical import (
    HistoricalReconstruction,
    build_historical_metrics_as_of,
    reconstruct_historical_score,
)
from stock_screener.backtest.historical_ingest import (
    CohortEntry,
    CohortIngestReport,
    HistoricalIngestResult,
    HistoricalMarketData,
    HistoricalObservationSource,
    PriceBackfillReport,
    PriceBackfillResult,
    backfill_historical_prices,
    backfill_historical_security_prices,
    ingest_cohort,
    ingest_historical_security,
)
from stock_screener.backtest.membership import (
    MEMBERSHIP_SOURCE,
    MembershipReconstruction,
    UniverseAsOfMembership,
    members_as_of,
    membership_for_security,
    reconstruct_membership_record,
)
from stock_screener.backtest.outcomes import (
    TERMINAL_MATCH_GRACE_DAYS,
    IdentityKind,
    OutcomeCoverage,
    OutcomeRow,
    SurvivorshipAudit,
    outcome_coverage,
    outcome_rows,
    resolve_terminal_outcome,
    survivorship_audit,
)
from stock_screener.backtest.scores import (
    ReconstructedScore,
    benchmark_returns_as_of,
    reconstruct_score,
)
from stock_screener.backtest.universe import reconstruct_universe

__all__ = [
    "MEMBERSHIP_SOURCE",
    "TERMINAL_MATCH_GRACE_DAYS",
    "BackfillReport",
    "Cadence",
    "ChainAccounting",
    "ChainStage",
    "ChainVerdict",
    "CohortEntry",
    "CohortIngestReport",
    "DateCoverage",
    "DateResult",
    "DateStatus",
    "DelistingFate",
    "ForwardOutcome",
    "HistoricalIngestResult",
    "HistoricalMarketData",
    "HistoricalObservationSource",
    "HistoricalPriceData",
    "HistoricalReconstruction",
    "IdentityKind",
    "MembershipReconstruction",
    "OutcomeCoverage",
    "OutcomeRow",
    "PriceBackfillReport",
    "PriceBackfillResult",
    "ReconstructedScore",
    "ResearchWindow",
    "SecurityReadiness",
    "SurvivorshipAudit",
    "UniverseAsOfMembership",
    "backfill_historical_prices",
    "backfill_historical_security_prices",
    "backfill_returns",
    "backfill_signals",
    "benchmark_returns_as_of",
    "build_historical_metrics_as_of",
    "chain_accounting",
    "date_coverage",
    "feature_coverage",
    "ingest_cohort",
    "ingest_historical_security",
    "market_sessions",
    "members_as_of",
    "membership_for_security",
    "outcome_coverage",
    "outcome_rows",
    "reconstruct_historical_score",
    "reconstruct_membership_record",
    "reconstruct_score",
    "reconstruct_universe",
    "research_window",
    "resolve_terminal_outcome",
    "sample",
    "security_readiness",
    "survivorship_audit",
]
