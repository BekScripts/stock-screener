"""Building, writing, verifying and mirroring `BACKTEST_DATA_CONTRACT_V1`.

## What a backtest binds to

`ContractBindings` is the set of digests a research run must present and the
contract must carry; `load_contract` refuses a contract file whose bindings
differ from what the caller demands, so research code cannot run against
whatever database happens to be present. The bindings are the surviving
hashes of this project — the frozen universe, the final evidence manifest,
the post-A3 index, the PIT completion marker, the corrected planning
inputs, the authorised plan — plus the semantic fingerprints of the tables
a historical observation is assembled from, computed here over canonical
projections and never over SQLite pages.

## What the contract asserts

Every statement is a rule the code already enforces, with the module that
owns it named beside it, or a limitation the code already documents. The
contract adds no semantics; it removes the possibility of not knowing them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import structlog

from domain.outcomes import DEFAULT_STALE_SESSIONS, FORWARD_HORIZONS
from domain.scores import COMPOUNDER_V1_2, CURRENT_SCORE_VERSION
from domain.signals import CURRENT_SIGNAL_VERSION, MARKET_SIGNALS_V1, MARKET_SIGNALS_V2
from stock_screener.pit.capture import FEATURE_VERSION
from stock_screener.pit.decision_time import MARKET_CLOSE, MARKET_OPEN, MARKET_TIMEZONE
from stock_screener.sec_evidence.plan import (
    CARRY_FORWARD_DAYS,
    PLAN_VERSION,
    WINDOW_END,
    WINDOW_START,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "CONTRACT_DIR",
    "CONTRACT_FILES",
    "CONTRACT_MIRROR_PREFIX",
    "CONTRACT_VERSION",
    "DATASET_ID",
    "ContractBindings",
    "ContractError",
    "build_contract",
    "fingerprint_dataset",
    "load_contract",
    "mirror_contract",
    "readiness_matrix",
    "run_manifest_schema",
    "write_contract",
]

DATASET_ID = "PIT_RESEARCH_DATASET_V2"
CONTRACT_VERSION = "BACKTEST_DATA_CONTRACT_V1"
EVIDENCE_GENERATION = "SEC_EVIDENCE_GENERATION_V2"
#: Beneath the control root.
CONTRACT_DIR = "contracts/backtest_v1"
CONTRACT_MIRROR_PREFIX = "backtest-contract"
CONTRACT_FILES = (
    "backtest_data_contract_v1.json",
    "backtest_dataset_manifest.json",
    "backtest_readiness_matrix.json",
    "historical_time_semantics.json",
    "feature_label_separation.json",
    "backtest_run_manifest_schema.json",
    "backtest_readiness_summary.json",
    "backtest_contract_complete.json",
)
#: The frozen research horizon: the months `research_universe_months` holds.
RESEARCH_HORIZON = {"first_month": "2022-01-01", "last_month": "2026-07-01", "months": 55}

#: The canonical projections whose digests define the dataset. Each is a total
#: order; autoincrement ids and observation timestamps are never part of a row.
FINGERPRINT_QUERIES: dict[str, str] = {
    "historical_universe": (
        "SELECT company_id, historical_security_id, symbol, observation_month, venue, state,"
        " security_class, class_evidence_mode, class_source_accession, class_evidence_as_of,"
        " eligible, eligibility_reasons FROM research_universe_months"
        " ORDER BY observation_month, company_id, historical_security_id, symbol"
    ),
    "universe_membership": (
        "SELECT company_id, historical_security_id, state, confidence, reason, valid_from,"
        " valid_to, inferred_inputs, source FROM universe_membership"
        " ORDER BY company_id, historical_security_id, valid_from, valid_to, state"
    ),
    "security_identifiers": (
        "SELECT historical_security_id, cusip, ticker, venue, valid_from, valid_to, source,"
        " confidence, lineage FROM security_identifiers"
        " ORDER BY historical_security_id, valid_from, valid_to, cusip, ticker"
    ),
    "fundamental_observations": (
        "SELECT company_id, taxonomy, concept, value, unit, period_start, period_end,"
        " filed_date, accession, form, fiscal_year, fiscal_period, frame"
        " FROM fundamental_observations"
        " ORDER BY company_id, filed_date, accession, taxonomy, concept, period_start,"
        " period_end, unit, frame, value"
    ),
    "filing_acceptance": (
        "SELECT accession, accepted_at, filed_date, form FROM filing_acceptance ORDER BY accession"
    ),
    "historical_observations": (
        "SELECT historical_security_id, taxonomy, concept, value, unit, period_start,"
        " period_end, filed_date, accession, form FROM historical_observations"
        " ORDER BY historical_security_id, filed_date, accession, taxonomy, concept,"
        " period_start, period_end, unit, value"
    ),
    "price_history": (
        "SELECT company_id, date, open, high, low, close, volume FROM price_history"
        " ORDER BY company_id, date"
    ),
    "historical_prices": (
        "SELECT historical_security_id, date, open, high, low, close, volume"
        " FROM historical_prices ORDER BY historical_security_id, date"
    ),
    "benchmark_prices": (
        "SELECT symbol, date, open, high, low, close, volume FROM benchmark_prices"
        " ORDER BY symbol, date"
    ),
    "corporate_actions": (
        "SELECT company_id, ticker, action_type, ex_date, split_factor, cash_amount, currency,"
        " record_date, payable_date FROM corporate_actions"
        " ORDER BY company_id, ex_date, action_type, split_factor, cash_amount"
    ),
    "delistings": (
        "SELECT ticker, kind, effective_date, cash_per_share, acquirer_ticker, acquirer_rate,"
        " acquiree_rate FROM delistings ORDER BY ticker, effective_date, kind"
    ),
    "feature_observations": (
        "SELECT company_id, historical_security_id, snapshot_date, feature_version,"
        " feature_as_of, bars_available, benchmark_symbol, last_close, atr_pct_14, atr_pct_20,"
        " atr_pct_60, rvol_5_pct, rvol_20_pct, rvol_60_pct, rvol_252_pct, vol_expansion_ratio,"
        " avg_abs_ret_20_pct, avg_abs_ret_60_pct, avg_abs_ret_252_pct, median_abs_ret_252_pct,"
        " max_abs_ret_60_pct, max_abs_ret_252_pct, days_over_1pct_share, days_over_2pct_share,"
        " days_over_3pct_share, median_range_pct, range_over_1pct_share, range_over_2pct_share,"
        " flat_bar_share, zero_move_share, closing_band_pct, ret_5d_pct, ret_20d_pct,"
        " ret_60d_pct, ret_252d_pct, trend_r2, trend_slope, above_sma20_share, high_52w, low_52w,"
        " distance_from_52w_high, gap_over_1pct_share, gap_over_2pct_share, mean_abs_gap_pct,"
        " max_abs_gap_pct, venue_dollar_volume_median, venue_dollar_volume_mean,"
        " venue_volume_stability, venue_volume_ratio, beta_60, correlation_60,"
        " rel_strength_20_pct, rel_strength_60_pct, has_extreme_move FROM pit_feature_snapshots"
        " ORDER BY snapshot_date, company_id, historical_security_id, feature_version"
    ),
    "signal_observations": (
        "SELECT company_id, historical_security_id, snapshot_date, signal_version,"
        " feature_version, feature_as_of, bars_available, peer_population, compression_ratio,"
        " volatility_expansion_ratio, distance_from_52w_high, distance_from_52w_low, stretch_20d,"
        " trend_direction, trend_r2, percentiles_json, setups, setup_evidence_json, tradability,"
        " tradability_reasons_json, liquidity_floor_proven FROM pit_signal_snapshots"
        " ORDER BY snapshot_date, signal_version, company_id, historical_security_id"
    ),
    "forward_return_labels": (
        "SELECT company_id, historical_security_id, observation_date, horizon_sessions, status,"
        " entry_date, entry_price, exit_date, exit_price, total_return, last_seen_date,"
        " last_seen_price FROM forward_returns"
        " ORDER BY observation_date, company_id, historical_security_id, horizon_sessions"
    ),
}

#: Which fingerprints belong to which conceptual layer.
LAYERS: dict[str, tuple[str, ...]] = {
    "UNIVERSE": ("historical_universe", "universe_membership", "security_identifiers"),
    "FEATURES": (
        "fundamental_observations",
        "filing_acceptance",
        "historical_observations",
        "price_history",
        "historical_prices",
        "benchmark_prices",
        "corporate_actions",
        "feature_observations",
    ),
    "SIGNALS": ("signal_observations",),
    "LABELS_EVALUATION_ONLY": ("forward_return_labels", "delistings"),
}


class ContractError(RuntimeError):
    """A binding the contract requires is absent or does not match."""


@dataclass(frozen=True)
class ContractBindings:
    """The digests a contract carries and a research run must present."""

    dataset_id: str
    contract_version: str
    universe_sha256: str
    evidence_manifest_sha256: str
    post_a3_index_sha256: str
    pit_marker_sha256: str
    planning_inputs_sha256: str
    a3_plan_sha256: str
    pit_audit_marker_sha256: str
    database_sha256: str
    fingerprints: dict[str, dict[str, object]] = field(default_factory=dict)

    def differences(self, other: ContractBindings) -> list[str]:
        """The binding names on which `other` differs from these."""
        names = [
            "dataset_id",
            "contract_version",
            "universe_sha256",
            "evidence_manifest_sha256",
            "post_a3_index_sha256",
            "pit_marker_sha256",
            "planning_inputs_sha256",
            "a3_plan_sha256",
            "pit_audit_marker_sha256",
            "database_sha256",
        ]
        out = [n for n in names if getattr(self, n) != getattr(other, n)]
        if self.fingerprints and other.fingerprints and self.fingerprints != other.fingerprints:
            out.append("fingerprints")
        return out


# ----------------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------------


def _digest_rows(rows: Iterable[tuple[object, ...]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def fingerprint_dataset(
    db: sqlite3.Connection,
    *,
    only: Iterable[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, object]]:
    """Digest every canonical projection in `FINGERPRINT_QUERIES` over a read-only connection.

    Args:
        db: The research database, opened read-only.
        only: A subset of projection names, for a test or a partial check.
        progress: Receives one line per projection.

    Returns:
        `{name: {"rows": n, "sha256": digest}}`.
    """
    prints: dict[str, dict[str, object]] = {}
    for name, query in FINGERPRINT_QUERIES.items():
        if only is not None and name not in set(only):
            continue
        rows, sha = _digest_rows(db.execute(query))
        prints[name] = {"rows": rows, "sha256": sha}
        if progress is not None:
            progress(f"fingerprint {name:<26} rows {rows:>10} {sha[:16]}…")
    return prints


# ----------------------------------------------------------------------------
# The contract's content
# ----------------------------------------------------------------------------


def _time_semantics() -> dict[str, object]:
    return {
        "timestamps": {
            "filing_date": "The SEC filing date (a date). Planning metadata; never an availability "
            "test.",
            "accepted_at": "The SEC acceptance instant (UTC), from `filing_acceptance` (5,889,860 "
            "rows). "
            "The availability test for company fundamentals.",
            "period_end": "The fiscal period the figure describes. Never a proxy for availability.",
            "evidence_date": "For a PIT cover row, the acceptance date the row is evidence as of.",
            "market_date": "A trading session; bars are daily; the calendar is the benchmark's "
            "(SPY).",
            "research_as_of": "A date denoting the close of that session, 16:00 America/New_York "
            "(`stock_screener.pit.decision_time`).",
            "rebalance_date": "A research as-of date at which a portfolio decision is taken.",
            "forward_horizon": "1, 5, 20, 60, 120 sessions on the benchmark calendar.",
        },
        "decision_instant": {
            "rule": "as_of denotes a decision at the market close of that session",
            "market_close": MARKET_CLOSE.isoformat(),
            "market_open": MARKET_OPEN.isoformat(),
            "timezone": str(MARKET_TIMEZONE),
            "owner": "stock_screener.pit.decision_time.decision_instant",
        },
        "sec_availability": {
            "companies": {
                "rule": "a filing is available iff accepted_at <= decision_instant(as_of)",
                "owner": "data_access.FundamentalObservationRepository"
                ".available_as_of(accepted_by=)",
                "fallback_when_instant_unknown": "filed_date < as_of — may delay a filing by one "
                "session, can never reveal one early",
                "measured_basis": "61.0% of observations come from filings accepted after the "
                "close; "
                "filed_date <= as_of would reveal 3.5M early (Phase 8F)",
            },
            "historical_securities": {
                "rule": "filed_date <= as_of (date-level)",
                "owner": "data_access.HistoricalObservationRepository.available_as_of",
                "limitation": "no acceptance-instant test on this path; a filing accepted after "
                "the "
                "close of its filing date is visible to a close-of-day decision on that date",
                "contract_rule": "research code MUST apply filed_date < as_of for historical "
                "securities (one-session lag, never early) until the acceptance-instant test is "
                "extended to `historical_observations`",
                "classification": "PIT_SAFE_WITH_LAG",
            },
            "pit_cover_evidence": {
                "universe_verdict_rule": "a class observation may describe a month only if the "
                "filing was accepted at or before that month and within the occupant's listing "
                "tenure (`stock_screener.universe.build`, Phase 9K.7)",
                "coverage_overlay_rule": "a held common-stock observation supports a month-end iff "
                f"evidence_date <= month_end and month_end - evidence_date <= {CARRY_FORWARD_DAYS} "
                "days (`stock_screener.sec_evidence.inputs`, the planning rule)",
                "note": "the two rules differ by one month at the boundary; the universe verdict "
                "is the conservative one and is what `research_universe_months.state` holds",
            },
        },
        "same_day_filing": {
            "question": "can a filing accepted during session T be used for a decision on T?",
            "companies": "yes iff accepted_at <= 16:00 America/New_York on T; an acceptance at "
            "15:55 is usable at the close, not at a 15:30 decision (no intraday decision point "
            "is defined); an acceptance after 16:00 is usable from the next session",
            "historical_securities": "conservative: next session after filed_date, always",
            "intraday_precision": "NOT SUPPORTED — no intraday prices exist; the only decision "
            "points are CLOSE (default) and OPEN",
        },
        "execution": {
            "signal_observation_time": "close of as_of",
            "trade_execution_time": "the open of the first session strictly after as_of",
            "entry_price": "that session's open (`domain.outcomes._entry_session`, strict)",
            "exit_price": "the open of the session horizon_sessions later",
            "forbidden": "filling at the close whose information the decision used; "
            "same-close execution for a close-of-day signal",
            "rule_supported_by_data": "signal at close → trade next session open",
        },
        "forward_returns": {
            "owner": "domain.outcomes.forward_returns; measured by stock_screener.pit.returns",
            "horizons_sessions": list(FORWARD_HORIZONS),
            "calendar": "benchmark (SPY) sessions, never the security's own bars",
            "basis": "adjusted (adjustment=all): splits and distributions are in the return",
            "statuses": {
                "REALISED": "both ends priced",
                "TRUNCATED": "horizon extends past the price record",
                "INTERRUPTED": "the market kept trading and the security did not; no number",
                "UNPRICED": "our record lacks the bar; kept apart from INTERRUPTED",
                "NO_ENTRY": "no open on the first session after the observation",
            },
            "stale_after_sessions": DEFAULT_STALE_SESSIONS,
            "role": "LABELS / EVALUATION ONLY",
        },
        "rebalance_calendar": {
            "primitive": "the benchmark session calendar (`benchmark_prices`, SPY, 1,531 sessions "
            "2020-07-27..2026-08-28); a rebalance date is a session; month-end rebalance = the "
            "last "
            "session of each calendar month in that calendar",
            "frequencies_supported_by_data": ["monthly", "weekly", "daily"],
            "first_research_class": "monthly, at the last session's close",
            "not_defined_here": "the frequency a strategy uses",
        },
    }


def _identity_semantics() -> dict[str, object]:
    return {
        "canonical_observation_key": {
            "identity": "(kind, identity_id) — kind ∈ {COMPANY, HISTORICAL}; COMPANY → "
            "companies.id, "
            "HISTORICAL → historical_securities.id; exactly one is set on every dual-identity row "
            "(forward_returns, universe_membership, research_universe_months, pit_*_snapshots)",
            "time": "as_of (research decision date) or observation_month (universe verdict)",
            "owner": "domain.membership / data_access dual-identity check constraints (Phase "
            "9A/9B/9C)",
        },
        "ticker": "an attribute of a (security, date): a lease, never an identity; joins on a "
        "ticker are occupancy-aware (`stock_screener.sec_evidence.join.join_symbol`, "
        "`stock_screener.listing.attribution`) and may return AMBIGUOUS/UNRESOLVED",
        "cik": "the issuer, never the security; a CIK may hold several classes; a historical "
        "security may have no CIK (2,748 of 3,150 do not); co-registrants file one accession "
        "under several CIKs and stay distinct filings",
        "cusip": "a dated identifier of an issue, remade by a reverse split; the issuer base "
        "(characters 1 to 6) is a rule, never crossed",
        "security_class": "cover-page class per filing (`securities.security_class`); credit for "
        "a filing goes only to the common class it names (`reachability.py`)",
        "forbidden_joins": [
            "collapsing two securities because a ticker matches",
            "collapsing two securities because a CIK matches",
            "using today's companies row to label a historical month",
            "using historical_securities.last_trading_date as a symbol handover boundary",
        ],
        "known_contamination": "69 live `companies` rows are detected as holding two issuers "
        "under one reused ticker (`stock_screener.identity.contamination`); they are reported, "
        "never merged or split; research must exclude or handle them explicitly",
    }


def _missingness() -> dict[str, object]:
    return {
        "rule": "missing is not zero (ADR-0007): a value the data cannot support is None end to "
        "end; 0.0 means the company reported zero",
        "states": {
            "known_value": "a stored figure",
            "explicit_absence": "e.g. EvidenceVerdict.PRESENT/UNPROVEN has no ABSENT; a negative "
            "is "
            "recorded only where a source states it",
            "unknown": "None / UNKNOWN / UNVERIFIED / OPEN — its own state, never collapsed",
            "not_applicable": "e.g. a bank's leverage penalty: unassessed, recorded as such",
        },
        "universe_verdict": "research_universe_months.state ∈ {ELIGIBLE, NON_COMMON_OR_EXCLUDED, "
        "UNKNOWN}; UNKNOWN is not ineligible",
        "pit_coverage_overlay": "a month open after A3 means no held qualifying filing supports it "
        "under the 90-day rule — not that the security was unlisted; residual classes are in "
        "pit_audit/pit_residual_months.jsonl (R2..R9)",
        "membership": "MEMBER (INFERRED) / NON_MEMBER (OBSERVED or INFERRED) / UNKNOWN (OBSERVED); "
        "a MEMBER verdict carries undated attributes back and says so in inferred_inputs",
        "tradability": "TRADABLE / UNTRADABLE / UNVERIFIED — three-valued; UNVERIFIED never "
        "becomes either neighbour",
        "liquidity": "stored volume is single-venue (IEX); a floor cleared is proven, a floor "
        "failed proves nothing (liquidity_floor_proven)",
        "forward_return": "None unless status == REALISED; INTERRUPTED carries no number",
        "terminal_value": "UNKNOWN where no feed states a consideration (every receivership; "
        "Bed Bath); never a price",
        "forbidden": "generic fillna(0) / coalesce-to-false over any of the above",
    }


def _feature_label_separation() -> dict[str, object]:
    return {
        "layers": {
            "UNIVERSE": {
                "tables": [
                    *LAYERS["UNIVERSE"],
                    "companies (current roster; attributes undated)",
                    "historical_securities (identity + last-known attrs)",
                ],
                "owner": "stock_screener.universe, stock_screener.backtest.membership / universe",
            },
            "FEATURES": {
                "tables": list(LAYERS["FEATURES"]),
                "owner": "stock_screener.pit.metrics (fundamentals as of), "
                "stock_screener.pit.prices "
                "(bars_until), domain.metrics, domain.scoring",
            },
            "SIGNALS": {
                "tables": list(LAYERS["SIGNALS"]),
                "owner": "stock_screener.signals.derivation over pit_feature_snapshots only",
            },
            "PORTFOLIO_DECISIONS": {"tables": [], "owner": "R1 — not built"},
            "EXECUTION": {
                "tables": [
                    "benchmark_prices (calendar)",
                    "price_history / historical_prices (fills)",
                ],
                "owner": "R1 — contract only: next-session open after the decision close",
            },
            "LABELS_EVALUATION_ONLY": {
                "tables": [
                    *LAYERS["LABELS_EVALUATION_ONLY"],
                    "historical_securities.terminal_value_*",
                ],
                "owner": "stock_screener.pit.returns, stock_screener.backtest.outcomes",
            },
        },
        "label_fields": [
            "forward_returns.total_return",
            "forward_returns.exit_price",
            "forward_returns.exit_date",
            "forward_returns.status",
            "forward_returns.last_seen_price",
            "delistings.cash_per_share",
            "historical_securities.terminal_value_per_share",
        ],
        "reviewed_lagged_lookalikes": {
            "domain.metrics.total_return(bars, lookback_days)": "a trailing return over bars <= "
            "as_of — a feature, not a label",
            "pit_feature_snapshots.ret_5d_pct .. ret_252d_pct": "trailing returns to feature_as_of",
            "pit_signal_snapshots.*": "derived from features only",
        },
        "forbidden_importers_of_labels": [
            "domain.scoring",
            "domain.signals",
            "domain.metrics",
            "domain.eligibility",
            "stock_screener.scoring",
            "stock_screener.signals",
            "stock_screener.scanning",
            "stock_screener.pit.metrics",
            "stock_screener.pit.capture",
            "stock_screener.backtest.scores",
            "stock_screener.backtest.historical",
        ],
        "audit_result": "none of the above imports domain.outcomes or ForwardReturnRepository "
        "(static check, tests/integration/test_backtest_contract.py); forward returns are read by "
        "pit.returns (measurement), backtest.outcomes / harness / chain / coverage (readiness "
        "reporting: has_outcome flag and evaluation), identity.audit and the CLI",
    }


def _current_metadata_audit() -> dict[str, object]:
    return {
        "companies.exchange": {
            "use": "eligibility (supported venue) in live scoring and in backtest.scores "
            "reconstruction",
            "dated": False,
            "classification": "CURRENT_APPLIED_BACKWARD — every stored exchange carries the day "
            "capture "
            "first ran; membership therefore INFERRED with inferred_inputs naming it",
        },
        "companies.sector": {
            "use": "eligibility (bank exclusion) and profile; 623 of 5,769 populated",
            "dated": False,
            "classification": "CURRENT_APPLIED_BACKWARD — NOT PIT-safe; documented in "
            "backtest.scores",
        },
        "companies.is_active": {
            "use": "eligibility in the live path; backtest.historical decides activity by date "
            "instead (last_trading_date)",
            "dated": False,
            "classification": "CLOSED_FOR_HISTORICAL_SECURITIES, CURRENT for live companies",
        },
        "companies.statement_profile": {
            "use": "reconstruction profile",
            "dated": False,
            "classification": "CURRENT_APPLIED_BACKWARD (documented gap)",
        },
        "current_ticker_map": {
            "use": "frozen filer resolution (B2.10) — fetched once; 135 delisted current-map "
            "matches deliberately declined",
            "classification": "FROZEN_INPUT, not consulted at research time",
        },
        "current_market_cap / current_liquidity / latest_company_facts": {
            "use": "live screener only (financial_snapshots, score_snapshots)",
            "classification": "NOT_USED by the PIT path; the PIT path reads "
            "fundamental_observations "
            "and bars_until(as_of)",
        },
        "latest_value_queries_in_historical_paths": {
            "backtest.universe._last_bar_by_company / backtest.membership": "MAX(price_history"
            ".date) "
            "per company — the record's own reach, used to adjudicate a ticker-keyed delisting and "
            "to bound membership intervals; not a feature value; classified RECORD_EXTENT",
            "backtest.coverage._sessions_before": "ORDER BY date DESC LIMIT n bounded by "
            "date <= as_of — correct",
            "pit.prices.bars_until": "bounded on or before as_of — correct",
        },
        "verdict": "no current or future metadata silently backfills a historical feature value; "
        "current exchange, sector and statement profile reach the eligibility verdict and are "
        "recorded as CURRENT_APPLIED_BACKWARD limitations",
    }


def _price_contract() -> dict[str, object]:
    return {
        "source": "Alpaca free feed, IEX venue (single venue, adjustment=all)",
        "tables": {
            "price_history": "companies; 6,817,179 bars, 5,765 companies, 2020-02-06..2026-08-28",
            "historical_prices": "delisted securities; 1,466,908 bars, 2,569 securities, "
            "2017-09-19..2026-08-26; fetched bounded to the security's own identifier era",
            "benchmark_prices": "SPY; 1,531 sessions 2020-07-27..2026-08-28; the session calendar",
        },
        "frequency": "daily bars (open, high, low, close, volume)",
        "adjustment": "adjustment=all — historical closes in today's share basis with "
        "distributions "
        "removed; correct for returns, wrong for market capitalisation",
        "as_traded_recovery": "splits undone exactly from corporate_actions "
        "(`domain.pricing.as_traded_price`, market_cap_as_of anchored on the share count's date); "
        "distributions not undone — a recovered price is a LOWER BOUND (~3% for AAPL over six "
        "years)",
        "splits": "SUPPORTED (corporate_actions: 288 forward, 1,527 reverse; historical: 22 / 192)",
        "dividends": "PARTIALLY_SUPPORTED — 80,200 cash dividends recorded; in adjusted returns by "
        "construction; not reversible for as-traded prices",
        "missing_sessions": "UNPRICED status on outcomes; features require strict windows "
        "(`_60` uses exactly 60 sessions or None)",
        "delisted_coverage": "2,569 of 3,150 historical securities have bars",
        "timezone_session": "America/New_York sessions; daily bars carry no intraday time",
        "volume": "single-venue IEX: 0.006% to 12% of consolidated; 11.5% of company bars and "
        "28.7% of historical bars carry zero volume; liquidity floors read one direction only",
        "feeds_never_mixed": "IEX and SIP differ 36x on volume; price_history records no "
        "provenance, so no partial SIP backfill",
        "pre_2020": "beyond the free feed's reach for companies; historical_prices reach 2017",
        "readiness": "READY_WITH_DOCUMENTED_LIMITATION",
    }


def _corporate_actions() -> dict[str, object]:
    return {
        "splits": "SUPPORTED — appended from the provider, never inferred from a price",
        "reverse_splits": "SUPPORTED (1,527 + 192)",
        "dividends": "PARTIALLY_SUPPORTED — recorded (80,200); embedded in adjusted returns; not "
        "reversible for as-traded prices",
        "mergers": "SUPPORTED_AS_TERMINAL_EVENTS — delistings: STOCK_MERGER 3,161, CASH_MERGER "
        "1,953, "
        "STOCK_AND_CASH_MERGER 246; matched to a security by identifier interval, never by symbol",
        "symbol_changes": "PARTIALLY_SUPPORTED — NAME_CHANGE 211 in corporate_actions; ticker "
        "handovers from the fails-to-deliver record (security_identifiers)",
        "spinoffs": "UNSUPPORTED — no source wired",
        "delistings": "SUPPORTED_WITH_LIMITATION — 5,796 registry rows by date range; "
        "WORTHLESS_REMOVAL 436; receivership is NOT covered (SVB, First Republic: UNKNOWN)",
        "delisting_return": "INTERRUPTED carries no number; a terminal value is a reported "
        "consideration (291 of 3,150 historical securities have one) or UNKNOWN — never the last "
        "price; a backtest must model the terminal leg explicitly or exclude INTERRUPTED "
        "positions with a stated rule; -100% and 0% are both forbidden defaults",
    }


def _universe_filters() -> dict[str, object]:
    return {
        "price >= $2": {
            "classification": "PIT_READY",
            "how": "last close on or before as_of (bars_until), adjusted basis; as-traded via "
            "splits",
        },
        "market_cap >= $100M": {
            "classification": "PARTIAL",
            "how": "market_cap_as_of(share count filed by as_of, as-traded price recovered by "
            "splits) — a lower bound because distributions are not undone",
        },
        "20D_average_dollar_volume >= $1M consolidated": {
            "classification": "PARTIAL",
            "how": "stored volume is single-venue IEX; clearing the floor is proven, failing it is "
            "UNVERIFIED (never an exclusion in research without a stated rule)",
        },
        "listing_status": {
            "classification": "PARTIAL",
            "how": "membership intervals (universe_membership) — MEMBER always INFERRED because "
            "venue is undated; research_universe_months carries the listing-observation view "
            "(listing_observations, 458,708 rows, 2022-01..2026-07)",
        },
        "supported_exchange": {"classification": "PARTIAL", "how": "companies.exchange is undated"},
        "sector": {
            "classification": "NOT_READY",
            "how": "current sector only, 623/5,769 populated; the bank exclusion applies today's "
            "label backward",
        },
        "min_trading_days": {"classification": "PIT_READY", "how": "bars_until(as_of) count"},
    }


def _compounder_contract() -> dict[str, object]:
    return {
        "version": COMPOUNDER_V1_2,
        "current_score_version": CURRENT_SCORE_VERSION,
        "frozen": True,
        "components": {
            "growth_35": [
                "revenue_growth_yoy 12",
                "revenue_growth_acceleration 8",
                "revenue_cagr_3y 6",
                "gross_profit_growth_yoy 5",
                "growth_persistence 4",
            ],
            "financial_quality_25": [
                "gross_margin_level_and_trend 7",
                "fcf_margin 7",
                "cash_against_debt 6",
                "operating_margin_improvement 5",
            ],
            "valuation_25": [
                "multiple (EV/revenue or P/S)",
                "growth_adjusted_valuation",
                "fcf_yield",
            ],
            "market_confirmation_15": [
                "relative_strength_6m 6",
                "relative_strength_12m 6",
                "position_52w 3",
            ],
            "risk_penalties_0_to_minus_25": [
                "dilution 0..-10",
                "cash_runway 0..-10",
                "balance_sheet 0..-5",
                "liquidity 0 (unpriced in v1)",
            ],
        },
        "missing_data": "weight redistributed inside the component only, capped at 1.15 (A1); "
        "a component with <50% of its weight available is unassessed; data_coverage recorded",
        "score_range": "0..100 after penalties",
        "v1_2_policy": "a score on STALE fundamentals keeps its number and is excluded from "
        "current "
        "rankings; freshness is recorded on the snapshot",
        "as_of_semantics": "reconstructed via stock_screener.backtest.scores: periods_as_of "
        "(acceptance instants), bars_until(as_of), market_cap_as_of; formula unchanged",
        "inputs": {
            "fundamental_observations (revenue, margins, FCF, cash, debt, shares)": "PIT_SAFE — "
            "acceptance-instant selection for companies",
            "historical_observations (delisted securities)": "PIT_SAFE_WITH_LAG — filed_date <= "
            "as_of; "
            "apply filed_date < as_of",
            "price bars for relative strength and 52-week position": "PIT_SAFE — bars_until(as_of)",
            "benchmark returns": "PIT_SAFE — benchmark_prices bounded by as_of",
            "market_cap": "PIT_SAFE_WITH_LAG — lower bound (distributions not undone)",
            "profile.exchange / sector / statement_profile": "NOT_CURRENTLY_BACKTEST_READY — "
            "current "
            "row applied backward (eligibility and bank exclusion)",
            "profile.is_active": "PIT_SAFE for historical securities (by date); CURRENT for live "
            "rows",
            "liquidity (ADV)": "PIT_SAFE_WITH_LAG — single-venue floor, one direction",
        },
        "persisted_history": "pit_score_snapshots hold 17,307 captured scores over 2026-08-26..28 "
        "only; every earlier score is a reconstruction (not a capture) and is labelled so",
    }


def _signals_contract() -> dict[str, object]:
    return {
        "version": MARKET_SIGNALS_V1,
        "current_signal_version": CURRENT_SIGNAL_VERSION,
        "feature_version": FEATURE_VERSION,
        "frozen": True,
        "inputs": "pit_feature_snapshots only (never score_snapshots, financial_snapshots or "
        "companies); features computed from bars_until(as_of) with strict windows",
        "timestamps": "feature_as_of <= snapshot_date by construction; a feature row whose "
        "feature_as_of == snapshot_date is the V2 trading-evidence test",
        "same_session_assumption": "a signal for D is a statement about D's close; acting on it "
        "means the next session's open",
        "future_leakage": "none found: derivation reads the feature record only, and the feature "
        "record is bounded by as_of",
        "peers": "V1 percentiles over the securities with a feature row on D; MARKET_SIGNALS_V2 "
        "(363,697 rows, 2022-01-31..2026-03-31, not promoted) narrows peers to "
        "members_as_of(D) ∩ market data on D with zero classification changes on peer-independent "
        "fields",
        "tradability": "three-valued; liquidity floor one-directional",
        "no_composite": "no score column exists and none may be added",
        "thresholds": "provisional, not fitted to returns; live in SignalThresholds",
    }


def readiness_matrix() -> list[dict[str, object]]:
    """The subsystem-by-subsystem readiness classification, with why/impact/fix for blockers."""
    return [
        {
            "subsystem": "historical_universe",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "research_universe_months (268,780 identity-months, dual identity) is the "
            "frozen "
            "monthly universe; MEMBER verdicts are INFERRED because venue/type/sector are undated",
            "impact": "eligibility on a past date carries today's exchange/sector backward",
            "minimum_fix": "a dated venue source to promote membership to OBSERVED (not required "
            "for R1)",
        },
        {
            "subsystem": "sec_pit_evidence",
            "status": "READY",
            "why": "final V2 corpus and PIT materialisation frozen; 86,649 residual months "
            "classified; "
            "UNKNOWN preserved as a state",
            "impact": "36.2% of previously-open months remain UNKNOWN under the evidence rule",
            "minimum_fix": "none — accepted property of V2",
        },
        {
            "subsystem": "fundamental_features",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "companies: acceptance-instant availability; historical securities: filed_date "
            "<= as_of",
            "impact": "same-day after-close filings visible to a close-of-day decision for "
            "delisted names",
            "minimum_fix": "research applies filed_date < as_of for historical securities; later "
            "extend "
            "acceptance instants to historical_observations",
        },
        {
            "subsystem": "compounder_score",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "frozen V1.2 reconstructed as of a date from PIT inputs; "
            "exchange/sector/statement "
            "profile read from the current row",
            "impact": "bank exclusion and venue eligibility use today's labels",
            "minimum_fix": "record the CURRENT_APPLIED_BACKWARD inputs per reconstructed score "
            "(already "
            "labelled a reconstruction)",
        },
        {
            "subsystem": "market_signals",
            "status": "READY",
            "why": "MARKET_SIGNALS_V1 over pit_feature_snapshots, feature_as_of <= snapshot_date",
            "impact": "IEX single-venue volume limits liquidity signals to one direction",
            "minimum_fix": "none",
        },
        {
            "subsystem": "price_history",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "daily adjusted bars 2020-02..2026-08 (companies), 2017-09..2026-08 (delisted); "
            "benchmark calendar",
            "impact": "no pre-2020 company bars; as-traded prices are lower bounds; single venue",
            "minimum_fix": "none for R1 monthly research",
        },
        {
            "subsystem": "corporate_actions",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "splits/reverse splits/dividends/name changes recorded; spinoffs unsupported",
            "impact": "a spinoff appears as a price drop in the adjusted series of the parent",
            "minimum_fix": "a spinoff source, if a strategy needs it",
        },
        {
            "subsystem": "forward_returns",
            "status": "READY",
            "why": "31,324,895 rows, 1/5/20/60/120 sessions, next-session-open entry, dual "
            "identity, "
            "five statuses",
            "impact": "INTERRUPTED carries no number (20,517 rows); TRUNCATED near the record's "
            "end",
            "minimum_fix": "none",
        },
        {
            "subsystem": "sector_industry",
            "status": "BLOCKED",
            "why": "only current sector (623/5,769) exists; no dated classification",
            "impact": "sector-neutral or sector-exposure research would use today's labels",
            "minimum_fix": "a dated sector source, or research declares sector NOT USED",
        },
        {
            "subsystem": "liquidity",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "single-venue IEX volume; floors one-directional",
            "impact": "a liquidity filter can only admit, never reject, with certainty",
            "minimum_fix": "consolidated volume (SIP) for a stated period — not mixed into "
            "price_history",
        },
        {
            "subsystem": "transaction_costs",
            "status": "PARTIAL",
            "why": "price and single-venue volume exist; no spread, no consolidated ADV",
            "impact": "only a stated-assumption cost model (bps of notional) is honest",
            "minimum_fix": "R1 chooses explicit cost scenarios and records them in the run "
            "manifest",
        },
        {
            "subsystem": "delisting_treatment",
            "status": "READY_WITH_DOCUMENTED_LIMITATION",
            "why": "delisted securities are ordinary rows everywhere; terminal values for 291 of "
            "3,150; "
            "INTERRUPTED never numbered; receivership UNKNOWN",
            "impact": "a backtest must state its terminal-leg rule; it may not default to 0 or "
            "-100%",
            "minimum_fix": "R1 chooses an explicit terminal-leg policy per scenario",
        },
        {
            "subsystem": "portfolio_constraints",
            "status": "NOT_REQUIRED_FOR_FIRST_BASELINE",
            "why": "volatility/correlation/beta features exist (PIT); regime (VIX) and sector are "
            "not PIT",
            "impact": "no portfolio-level risk logic may run on non-PIT inputs",
            "minimum_fix": "R1 defines constraints only over PIT inputs",
        },
    ]


def run_manifest_schema() -> dict[str, object]:
    """The JSON schema every future backtest run must emit."""
    required = [
        "run_id",
        "strategy_version",
        "code_commit",
        "dataset_id",
        "contract_version",
        "universe_fingerprint",
        "feature_fingerprints",
        "price_data_fingerprint",
        "label_fingerprint",
        "rebalance_rule",
        "execution_rule",
        "transaction_cost_assumptions",
        "terminal_leg_policy",
        "date_range",
        "parameters",
        "variants_tried",
        "selection_criterion",
        "benchmark",
        "output_fingerprints",
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "backtest_run_manifest",
        "type": "object",
        "required": required,
        "properties": {
            "run_id": {"type": "string"},
            "strategy_version": {"type": "string"},
            "code_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "dataset_id": {"const": DATASET_ID},
            "contract_version": {"const": CONTRACT_VERSION},
            "universe_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "feature_fingerprints": {"type": "object", "additionalProperties": {"type": "string"}},
            "price_data_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "label_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "rebalance_rule": {"type": "string"},
            "execution_rule": {"type": "string", "enum": ["CLOSE_SIGNAL_NEXT_SESSION_OPEN"]},
            "transaction_cost_assumptions": {"type": "object"},
            "terminal_leg_policy": {"type": "string"},
            "date_range": {"type": "object", "required": ["start", "end"]},
            "development_validation_holdout": {"type": "object"},
            "parameters": {"type": "object"},
            "variants_tried": {"type": "integer", "minimum": 1},
            "parameter_sweeps": {"type": "array"},
            "selection_criterion": {"type": "string"},
            "benchmark": {"type": "string"},
            "metrics": {"type": "object"},
            "output_fingerprints": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "additionalProperties": True,
    }


def _research_policy() -> dict[str, object]:
    return {
        "development_validation_holdout": "every parameterised or learned strategy declares three "
        "disjoint date ranges before its first run and records them in the run manifest; the "
        "final holdout is evaluated once per strategy version; boundaries are R1's decision, "
        "not chosen here",
        "multiple_testing": "variants_tried, parameter_sweeps and selection_criterion are required "
        "run-manifest fields; a reported result names the population it was selected from",
        "benchmark": "every run names a passive reference (SPY total return is the calendar source "
        "already stored; R1 chooses and records the benchmark); benchmark-relative return is a "
        "required metric",
        "required_metrics": [
            "CAGR",
            "annualised_volatility",
            "sharpe",
            "sortino",
            "max_drawdown",
            "calmar",
            "turnover",
            "hit_rate",
            "exposure",
            "cash_utilisation",
            "transaction_costs",
            "slippage",
            "benchmark_relative_return",
            "trades",
            "holdings",
            "concentration",
            "sector_exposure_where_pit",
        ],
        "execution_realism_minimums": [
            "trade only after signal availability (next session open)",
            "no zero-slippage assumption without an explicit scenario",
            "liquidity constraint stated",
            "position-size constraint stated",
            "cash constraint",
            "no fills at unavailable prices (UNPRICED / NO_ENTRY sessions)",
        ],
        "no_results_here": "B2.18 produces no strategy result; a smoke test's return is not a "
        "result",
    }


def _first_safe_class() -> dict[str, object]:
    return {
        "description": "monthly cross-sectional research on the frozen identity-month universe: "
        "signal at the last session's close of each month from PIT fundamentals, frozen "
        "COMPOUNDER_V1_2 reconstruction and MARKET_SIGNALS_V1; long-only, equal- or rank-weighted; "
        "delisted securities included as identities with INTERRUPTED outcomes handled by a stated "
        "rule",
        "allowed_signal_time": "close of the rebalance session (16:00 America/New_York)",
        "allowed_execution_time": "open of the next benchmark session",
        "supported_horizons_sessions": list(FORWARD_HORIZONS),
        "horizon_for_monthly": "20 sessions (≈ one month) from the entry open; 60 for a quarter",
        "material_limitations": [
            "exchange/sector eligibility uses today's labels (INFERRED membership)",
            "historical-security fundamentals need filed_date < as_of",
            "market cap is a lower bound; liquidity floors admit only",
            "no intraday, no same-close execution, no spinoff handling",
            "36.2% of previously-open universe months remain UNKNOWN in PIT evidence",
            "company bars begin 2020-02; research horizon 2022-01..2026-07",
        ],
        "not_supported_yet": [
            "daily intraday strategies",
            "sector-neutral construction",
            "consolidated-liquidity filters",
            "regime (VIX) overlays",
        ],
    }


def build_contract(
    bindings: ContractBindings,
    *,
    pit_summary: Mapping[str, object],
    audit_summary: Mapping[str, object],
    repo_commit: str | None,
) -> dict[str, dict[str, object]]:
    """Assemble every contract file's content from the bindings and the frozen facts.

    Args:
        bindings: The digests the contract is bound to, fingerprints included.
        pit_summary: The B2.16 `pit_final_summary.json` (counts quoted, never recomputed).
        audit_summary: The B2.17 `pit_residual_summary.json`.
        repo_commit: The repository commit the contract was frozen from.

    Returns:
        `{file name: content}` for every entry of `CONTRACT_FILES`.
    """
    core = {
        "dataset_id": bindings.dataset_id,
        "contract_version": bindings.contract_version,
        "evidence_generation": EVIDENCE_GENERATION,
        "lineage": [
            "SEC_EVIDENCE_GENERATION_V2 (evidence: 99,774 trusted, manifest "
            f"{bindings.evidence_manifest_sha256[:16]}…)",
            f"post-A3 derived index {bindings.post_a3_index_sha256[:16]}…",
            f"PIT materialisation (marker {bindings.pit_marker_sha256[:16]}…)",
            f"residual audit (marker {bindings.pit_audit_marker_sha256[:16]}…)",
            f"{DATASET_ID} under {CONTRACT_VERSION}",
        ],
        "bindings": {k: v for k, v in asdict(bindings).items() if k != "fingerprints"},
        "research_horizon": RESEARCH_HORIZON,
        "candidate_window": {
            "definition": "HISTORICAL_9L_V1",
            "start": WINDOW_START.isoformat(),
            "end": WINDOW_END.isoformat(),
            "carry_forward_days": CARRY_FORWARD_DAYS,
            "plan_version": PLAN_VERSION,
            "status": "FROZEN — acquisition scope, not point-in-time validity",
        },
        "acquisition_status": {
            "sec_evidence_acquisition": "FROZEN",
            "a4": "NONE — residual_sec_forensic_plan.jsonl is FORENSIC_ONLY / "
            "NOT_AUTHORIZED_FOR_ACQUISITION",
            "reopening": "only after an independently reviewed correctness defect",
            "forbidden": [
                "baseline expansion",
                "A3 continuation",
                "ticker-map refresh for historical reconstruction",
                "submissions refresh for the frozen window",
                "retrospective use of September 2026 information",
            ],
        },
        "pit_outcome": {
            k: pit_summary.get(k)
            for k in (
                "pre_open",
                "newly_resolved",
                "still_open",
                "regressed",
                "expected_solved",
                "actual_minus_expected",
                "future_invalid_edges",
                "predicted_solvable",
                "predicted_unsolvable",
            )
        },
        "residual_accepted": audit_summary.get("sufficiency"),
        "versions": {
            "compounder": COMPOUNDER_V1_2,
            "market_signals": MARKET_SIGNALS_V1,
            "market_signals_research": MARKET_SIGNALS_V2,
            "features": FEATURE_VERSION,
            "forward_returns": "domain.outcomes.forward_returns "
            f"(horizons {list(FORWARD_HORIZONS)}, stale_after {DEFAULT_STALE_SESSIONS})",
            "plan": PLAN_VERSION,
        },
        "repo_commit": repo_commit,
    }
    time_semantics = _time_semantics()
    separation = _feature_label_separation()
    matrix = readiness_matrix()
    blockers = [m for m in matrix if m["status"] == "BLOCKED"]
    verdict = (
        "READY_WITH_DOCUMENTED_LIMITATIONS"
        if not [
            m
            for m in blockers
            if m["subsystem"]
            in (
                "historical_universe",
                "sec_pit_evidence",
                "fundamental_features",
                "price_history",
                "forward_returns",
                "compounder_score",
                "market_signals",
            )
        ]
        else "BLOCKED"
    )
    contract = {
        **core,
        "observation_identity": _identity_semantics(),
        "time_semantics": time_semantics,
        "missingness": _missingness(),
        "current_metadata_audit": _current_metadata_audit(),
        "price_data": _price_contract(),
        "corporate_actions": _corporate_actions(),
        "universe_filters": _universe_filters(),
        "compounder_v1_2": _compounder_contract(),
        "market_signals_v1": _signals_contract(),
        "allowed_feature_sets": {
            "fundamentals": "fundamental_observations / historical_observations as of the decision "
            "instant (companies) or filed_date < as_of (historical)",
            "prices": "bars_until(as_of) from price_history / historical_prices / benchmark_prices",
            "features": "pit_feature_snapshots rows with snapshot_date <= as_of",
            "signals": "pit_signal_snapshots rows with snapshot_date <= as_of",
            "score": "COMPOUNDER_V1_2 reconstructed as of as_of (backtest.scores / historical)",
            "universe": "research_universe_months for observation_month <= as_of's month; "
            "universe_membership intervals covering as_of",
            "pit_evidence_overlay": "pit_month_outcomes.jsonl for month_end <= as_of",
        },
        "forbidden_label_sets": separation["label_fields"],
        "research_policy": _research_policy(),
        "first_safe_research_class": _first_safe_class(),
    }
    manifest = {
        "dataset_id": bindings.dataset_id,
        "contract_version": bindings.contract_version,
        "evidence_generation": EVIDENCE_GENERATION,
        "bindings": {k: v for k, v in asdict(bindings).items() if k != "fingerprints"},
        "fingerprints": bindings.fingerprints,
        "layers": LAYERS,
        "pit_outcome_fingerprints": pit_summary.get("fingerprints"),
        "analysis_horizon": RESEARCH_HORIZON,
        "versions": core["versions"],
        "repo_commit": repo_commit,
        "not_bound": [
            "market-signals thresholds (provisional, in SignalThresholds)",
            "a transaction-cost model (R1)",
            "a benchmark choice (R1)",
        ],
    }
    summary = {
        "dataset_id": bindings.dataset_id,
        "contract_version": bindings.contract_version,
        "readiness_verdict": verdict,
        "blocked": [m["subsystem"] for m in blockers],
        "ready": [m["subsystem"] for m in matrix if m["status"] == "READY"],
        "ready_with_limitation": [
            m["subsystem"] for m in matrix if m["status"] == "READY_WITH_DOCUMENTED_LIMITATION"
        ],
        "partial": [m["subsystem"] for m in matrix if m["status"] == "PARTIAL"],
        "not_required_for_first_baseline": [
            m["subsystem"] for m in matrix if m["status"] == "NOT_REQUIRED_FOR_FIRST_BASELINE"
        ],
        "first_safe_research_class": _first_safe_class()["description"],
        "fingerprint_rows": {k: v["rows"] for k, v in bindings.fingerprints.items()},
    }
    complete: dict[str, object] = {
        "stage": "BACKTEST_CONTRACT_COMPLETE",
        "dataset_id": bindings.dataset_id,
        "contract_version": bindings.contract_version,
        "evidence_generation": EVIDENCE_GENERATION,
        "source_evidence_manifest_sha256": bindings.evidence_manifest_sha256,
        "post_a3_index_sha256": bindings.post_a3_index_sha256,
        "pit_marker_sha256": bindings.pit_marker_sha256,
        "pit_audit_marker_sha256": bindings.pit_audit_marker_sha256,
        "universe_sha256": bindings.universe_sha256,
        "database_sha256": bindings.database_sha256,
        "core_fingerprints": {
            k: bindings.fingerprints[k]
            for k in (
                "historical_universe",
                "feature_observations",
                "price_history",
                "forward_return_labels",
            )
            if k in bindings.fingerprints
        },
        "readiness_verdict": verdict,
        "known_blockers": [
            {
                "subsystem": m["subsystem"],
                "why": m["why"],
                "impact": m["impact"],
                "minimum_fix": m["minimum_fix"],
            }
            for m in blockers
        ],
        "first_safe_research_class": _first_safe_class(),
        "sec_evidence_acquisition": "FROZEN",
        "next_program": "R1 — STRATEGY RESEARCH & BACKTESTING",
    }
    return {
        "backtest_data_contract_v1.json": contract,
        "backtest_dataset_manifest.json": manifest,
        "backtest_readiness_matrix.json": {
            "dataset_id": bindings.dataset_id,
            "contract_version": bindings.contract_version,
            "matrix": matrix,
        },
        "historical_time_semantics.json": {
            "dataset_id": bindings.dataset_id,
            "contract_version": bindings.contract_version,
            **time_semantics,
        },
        "feature_label_separation.json": {
            "dataset_id": bindings.dataset_id,
            "contract_version": bindings.contract_version,
            **separation,
        },
        "backtest_run_manifest_schema.json": run_manifest_schema(),
        "backtest_readiness_summary.json": summary,
        "backtest_contract_complete.json": complete,
    }


# ----------------------------------------------------------------------------
# Writing, loading, mirroring
# ----------------------------------------------------------------------------


def _write_json(path: Path, payload: Mapping[str, object]) -> str:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


def write_contract(directory: Path, files: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """Write every contract file deterministically, the completion marker last.

    Returns:
        SHA-256 per file, by name.
    """
    directory.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for name in CONTRACT_FILES:
        if name == "backtest_contract_complete.json":
            continue
        digests[name] = _write_json(directory / name, files[name])
    complete = dict(files["backtest_contract_complete.json"])
    complete["files"] = dict(digests)
    digests["backtest_contract_complete.json"] = _write_json(
        directory / "backtest_contract_complete.json", complete
    )
    for name, sha in digests.items():
        (directory / f"{name.rsplit('.', 1)[0]}.sha256").write_text(f"{sha}  {name}\n")
    log.info("backtest contract written", directory=str(directory), files=len(digests))
    return digests


def load_contract(directory: Path, *, expect: ContractBindings) -> dict[str, object]:
    """Load the contract and refuse it unless every binding matches. Fail closed.

    Args:
        directory: The contract directory.
        expect: What the caller has proven about its own inputs. Fingerprints
            are compared only when both sides carry them.

    Returns:
        The contract's content.

    Raises:
        ContractError: If the contract or its marker is missing, the marker
            does not name the files as written, or any binding differs.
    """
    path = directory / "backtest_data_contract_v1.json"
    marker_path = directory / "backtest_contract_complete.json"
    if not path.is_file() or not marker_path.is_file():
        raise ContractError(f"no complete backtest contract at {directory}")
    contract: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("stage") != "BACKTEST_CONTRACT_COMPLETE":
        raise ContractError("the completion marker does not mark a complete contract")
    recorded = marker.get("files") or {}
    for name, sha in recorded.items():
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != sha:
            raise ContractError(f"{name} does not hash as the completion marker recorded")
    raw = contract.get("bindings")
    if not isinstance(raw, dict):
        raise ContractError("the contract carries no bindings")
    manifest = json.loads(
        (directory / "backtest_dataset_manifest.json").read_text(encoding="utf-8")
    )
    held = ContractBindings(
        **{
            k: str(raw.get(k)) for k in ContractBindings.__dataclass_fields__ if k != "fingerprints"
        },
        fingerprints=dict(manifest.get("fingerprints") or {}),
    )
    drift = held.differences(expect)
    if drift:
        raise ContractError(f"the contract is bound to other inputs: {drift}; refusing to run")
    return contract


def mirror_contract(
    mirror: ControlMirror, directory: Path, *, generation_id: str
) -> list[MirrorRecord]:
    """Mirror every contract file as a snapshot under `backtest-contract/`, and read each back."""
    records = []
    for name in CONTRACT_FILES:
        local = directory / name
        if not local.is_file():
            raise ContractError(f"{local} does not exist; nothing to mirror")
        kind = f"{CONTRACT_MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise ContractError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records
