"""The value objects a score is expressed in.

A number on its own is not a score. `82` is only useful if the reader can see
that it came from 30.4 of 35 growth points earned on 38.2% revenue growth, that
the valuation multiple used was price-to-sales because debt was unknown, and
that the risk penalty was -4.6 for dilution. Everything here exists to carry
that structure from the engine to a CLI table, a CSV, an API response and,
later, a dashboard — without any of them parsing a formatted string.

Three rules shape the models:

**A missing input is visible.** A `SubScore` with `points is None` is a metric
the data could not support. It is never rendered as zero, and the component it
belongs to records that its weight was redistributed.

**A score is either earned or absent.** `final_score` is None whenever
`scoring_status` is not `SCORED`. There is no "provisional" number for a company
that could not be judged, because a number that exists will be ranked.

**The formula version travels with the score.** Comparing a score computed under
one set of rules with one computed under another measures the rules, not the
business, so every snapshot carries `score_version` and comparisons refuse to
cross it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from domain.models import _Frozen

COMPOUNDER_V1 = "COMPOUNDER_V1"
"""The original scoring rules.

Retained so historical snapshots can still be read and compared with each other.
Nothing computes a V1 score any more.
"""

COMPOUNDER_V1_1 = "COMPOUNDER_V1_1"
"""V1 plus three guards, and no change to any weight or curve.

Redistribution is capped at a 15% uplift, acceleration credit is withheld from a
company whose growth is a rebound against a flat three-year trend, and revenue
growth plus acceleration are capped for a company whose growth does not persist.
The four components are still 35/25/25/15 and the risk penalties are untouched.
"""

COMPOUNDER_V1_2 = "COMPOUNDER_V1_2"
"""V1.1 with one policy change, and no change to any number.

**Every formula, curve, weight, threshold and penalty is V1.1's.** Growth,
quality, valuation and momentum are computed identically, redistribution still
caps at 1.15, the component and coverage minimums are unchanged, the category
bands are unchanged, and the currency and cadence rules are untouched. A company
whose fundamentals are current scores exactly what it scored under V1.1, to the
last decimal — there is a test that asserts it.

What changed is which scores a **current** ranking may contain. A score built on
stale fundamentals is still a real score of the company as it last reported, and
it stays on the stock page, in research and in history. It is simply not an
answer to "what looks interesting *now*": Centerra Gold ranked twenty-eighth on
revenue from 2023 measured against a market capitalisation from 2026, which is a
mixed-vintage valuation in the same way a converted market cap over unconverted
statements is a mixed-currency one.

This needed a version because it changes what a ranking *is*, and rankings from
before and after are therefore not comparable — even though no company's number
moved.
"""

CURRENT_SCORE_VERSION = COMPOUNDER_V1_2
"""The version every new score is stamped with.

Changing a curve, a weight or a policy means a new identifier and a new value
here — never an edit to an existing one. Snapshots keep the version they were
computed under, and no comparison crosses versions; see ADR-0006.
"""


class ScoringStatus(StrEnum):
    """Whether a company has a score, and why not when it does not."""

    SCORED = "SCORED"
    """Every component was scored. `final_score` is a number."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    """A component could not reach its minimum data coverage. Not a judgement
    about the company — a statement about what is known of it."""

    UNSUPPORTED_SECTOR = "UNSUPPORTED_SECTOR"
    """A business whose economics the general model misreads — banks, insurers
    and similar. Kept in the universe, kept out of the ranking."""

    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    """The security failed the Phase 1 eligibility screen, so nothing downstream
    of it was calculated."""

    ERROR = "ERROR"
    """Scoring raised. Recorded rather than swallowed so one broken company is
    visible without ending the run."""


class Freshness(StrEnum):
    """Whether a score's fundamentals are recent enough to describe the company now.

    Recorded on the snapshot rather than worked out by whoever reads it. The
    bound depends on the company's reporting cadence — an annual filer is not
    stale eight months after its year end — so a reader comparing a date against
    a fixed window would get a different answer from the screen that produced the
    row, and two readers would get different answers from each other.
    """

    CURRENT = "CURRENT"
    """The newest statement is no older than the cadence explains."""

    STALE = "STALE"
    """The company has effectively skipped a reporting period. The score is still
    a real score of the company as it last reported, and it remains on the stock
    page, in research and in history — it is simply not an answer to what looks
    interesting *now*, because the market side of every ratio in it has moved on
    and the statements have not."""


def is_rank_eligible(status: ScoringStatus, freshness: Freshness) -> bool:
    """Whether a snapshot may appear in a **current** ranking.

    The whole of the V1.2 policy change, in one function, so that every ranking
    view answers the question the same way and none of them re-derives it.

    Args:
        status: The scoring status of the snapshot.
        freshness: Whether its fundamentals are current.

    Returns:
        True only for a scored company whose fundamentals are current. A stale
        score is deliberately *not* unscored: it keeps its number everywhere the
        number is presented as a description of the company rather than as a
        ranking of it.
    """
    return status is ScoringStatus.SCORED and freshness is Freshness.CURRENT


class ComponentStatus(StrEnum):
    """Whether one component of the score could be calculated."""

    SCORED = "SCORED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class RiskLevel(StrEnum):
    """A label derived deterministically from the total risk penalty."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


class ScoreCategory(StrEnum):
    """Research priority implied by the final score.

    These are reading instructions — which companies to look at first — and not
    buy or sell recommendations. The tool ranks candidates for research; it does
    not have a view on a security.
    """

    EXCEPTIONAL_RESEARCH_CANDIDATE = "EXCEPTIONAL_RESEARCH_CANDIDATE"
    STRONG_RESEARCH_CANDIDATE = "STRONG_RESEARCH_CANDIDATE"
    WORTH_WATCHING = "WORTH_WATCHING"
    MIXED = "MIXED"
    LOW_PRIORITY = "LOW_PRIORITY"


class ValuationBasis(StrEnum):
    """Which multiple the valuation component actually used.

    Price-to-sales and EV-to-revenue are not the same measure, and a company
    scored on the fallback must not be read as though its balance sheet was
    taken into account.
    """

    EV_TO_REVENUE = "EV_TO_REVENUE"
    """Enterprise value over trailing revenue. Preferred: it accounts for debt
    and cash."""

    PRICE_TO_SALES = "PRICE_TO_SALES"
    """Market cap over trailing revenue. Used when debt or cash is unknown, so
    enterprise value cannot be computed."""

    NOT_AVAILABLE = "NOT_AVAILABLE"
    """Neither could be computed."""


class MetricUnit(StrEnum):
    """How an observed value should be read.

    A renderer needs this to tell `0.15` meaning 15% from `0.15` meaning +15
    percentage points of acceleration — the confusion this project is most
    exposed to, since both appear in the same table.
    """

    PERCENT = "PERCENT"
    """A decimal proportion. 0.35 is 35%."""

    POINTS = "POINTS"
    """A decimal difference between two rates. 0.15 is +15pp."""

    MULTIPLE = "MULTIPLE"
    """A ratio read as a multiple, e.g. 3.2x."""

    COUNT = "COUNT"
    """A whole number of observations."""

    MONTHS = "MONTHS"
    """A duration in months."""

    MONEY = "MONEY"
    """An amount in whole dollars."""


class ScoreWarning(StrEnum):
    """A caveat about a score that does not change the number.

    Warnings describe the *data*, not the company. Anything that should change
    the score is a penalty or a missing sub-score instead.
    """

    WEIGHT_REDISTRIBUTED = "WEIGHT_REDISTRIBUTED"
    """A component scored on less than its full set of metrics; the available
    ones carried the component's full weight."""

    VALUATION_FALLBACK_PRICE_TO_SALES = "VALUATION_FALLBACK_PRICE_TO_SALES"
    """Enterprise value was unavailable, so valuation used price-to-sales."""

    DILUTION_NOT_ASSESSED = "DILUTION_NOT_ASSESSED"
    """Share count history did not support a dilution figure, so no dilution
    penalty was applied — and none was earned either."""

    RUNWAY_NOT_ASSESSED = "RUNWAY_NOT_ASSESSED"
    """Cash or trailing free cash flow was unavailable, so cash runway is
    unknown rather than adequate."""

    LEVERAGE_NOT_ASSESSED = "LEVERAGE_NOT_ASSESSED"
    """Debt, cash or market cap was unavailable, so leverage is unknown."""

    LIQUIDITY_UNVERIFIED = "LIQUIDITY_UNVERIFIED"
    """Only partial-market volume was available. Carried through as a statement
    about the data; it is not a scoring input."""

    NO_BENCHMARK = "NO_BENCHMARK"
    """No benchmark return was available, so relative strength could not be
    calculated."""


class BenchmarkReturns(_Frozen):
    """Broad-market returns to measure relative strength against.

    Attributes:
        symbol: The benchmark's ticker, recorded so a score can say what it was
            measured against.
        return_6m: The benchmark's six-month return, as a decimal.
        return_12m: The benchmark's twelve-month return, as a decimal.
    """

    symbol: str
    return_6m: float | None = None
    return_12m: float | None = None


class SubScore(_Frozen):
    """One metric's contribution to a component.

    Attributes:
        name: Machine-readable identifier, stable across versions of the UI.
        points: Points earned, or None when the metric was unavailable. Never
            0.0 for a metric that could not be calculated.
        max_points: The most this metric can contribute before redistribution.
        observed: The metric the points were calculated from, for display.
        unit: How to read `observed`.
        note: A short explanation where the rule needs one, e.g. which
            adjustment applied.
    """

    name: str
    points: float | None
    max_points: float
    observed: float | None = None
    unit: MetricUnit = MetricUnit.PERCENT
    note: str | None = None

    @property
    def available(self) -> bool:
        """Whether the metric could be scored at all."""
        return self.points is not None


class ComponentScore(_Frozen):
    """One of the four positive components of the score.

    Attributes:
        name: Component identifier — `growth`, `quality`, `valuation`,
            `momentum`.
        status: Whether the component reached its minimum data coverage.
        score: Points earned out of `max_points`, or None when it did not.
        max_points: The component's fixed contribution to the 100-point raw
            score. Never varies with what data was available.
        coverage: Share of the component's weight that could be scored, 0-1.
        subscores: Every metric considered, including the unavailable ones.
        redistributed: Whether an unavailable metric's weight was carried by the
            available ones.
    """

    name: str
    status: ComponentStatus
    score: float | None
    max_points: float
    coverage: float = Field(ge=0, le=1)
    subscores: tuple[SubScore, ...] = ()
    redistributed: bool = False

    @property
    def missing_metrics(self) -> tuple[str, ...]:
        """Names of the metrics this component could not score."""
        return tuple(sub.name for sub in self.subscores if not sub.available)


class RiskAssessment(_Frozen):
    """The penalties applied after the raw score, and what they were based on.

    Penalties are zero or negative. A penalty of `None` means the risk could not
    be assessed — which is neither a penalty nor a clean bill of health, and is
    why `coverage` is reported beside the total.

    Attributes:
        dilution_penalty: For share count growth.
        runway_penalty: For months of cash left at the current burn.
        balance_sheet_penalty: For severe leverage only; ordinary balance-sheet
            quality is scored inside the quality component instead.
        liquidity_penalty: Always 0.0 in v1 — see the module documentation for
            why unverified liquidity is a warning rather than a penalty.
        total_penalty: The penalties summed and floored at `max_total_penalty`.
        level: The label derived from `total_penalty`.
        coverage: Share of the assessable risks that could be assessed, 0-1.
        share_count_growth_yoy: Observed dilution, for display.
        cash_runway_months: Observed runway, for display. None for a company
            generating cash — it has no finite runway, which is not a gap.
        net_debt_to_market_cap: Observed leverage, for display.
        warnings: Risks that could not be assessed.
    """

    dilution_penalty: float | None = None
    runway_penalty: float | None = None
    balance_sheet_penalty: float | None = None
    liquidity_penalty: float = 0.0
    total_penalty: float = 0.0
    level: RiskLevel = RiskLevel.LOW
    coverage: float = Field(default=0.0, ge=0, le=1)
    share_count_growth_yoy: float | None = None
    cash_runway_months: float | None = None
    net_debt_to_market_cap: float | None = None
    warnings: tuple[ScoreWarning, ...] = ()


class CompanyScore(_Frozen):
    """A complete, explainable CompounderScore for one company.

    Attributes:
        ticker: The company scored.
        score_version: The rules used. Scores from different versions are not
            comparable and must never be subtracted from one another.
        status: Whether a number was produced, and why not when it was not.
        growth: The growth component, or None when scoring stopped before it.
        quality: The financial quality component.
        valuation: The valuation component.
        momentum: The market confirmation component.
        raw_score: The four components summed, 0-100, before risk.
        risk: The penalties and their basis.
        final_score: `raw_score` plus the risk penalty, clamped to 0-100.
        category: The research priority band `final_score` falls in.
        data_coverage: Share of all scoring metrics that were available, 0-1.
            Reported beside the score rather than folded into it: incomplete
            data should be visible, not silently punished.
        valuation_basis: Which multiple the valuation component used.
        warnings: Caveats about the data behind the score.
    """

    ticker: str
    score_version: str = CURRENT_SCORE_VERSION
    status: ScoringStatus = ScoringStatus.SCORED

    growth: ComponentScore | None = None
    quality: ComponentScore | None = None
    valuation: ComponentScore | None = None
    momentum: ComponentScore | None = None

    raw_score: float | None = None
    risk: RiskAssessment | None = None
    final_score: float | None = None
    category: ScoreCategory | None = None
    data_coverage: float | None = None
    valuation_basis: ValuationBasis = ValuationBasis.NOT_AVAILABLE
    warnings: tuple[ScoreWarning, ...] = ()

    @property
    def components(self) -> tuple[ComponentScore, ...]:
        """Every component that was calculated, in score order."""
        return tuple(
            component
            for component in (self.growth, self.quality, self.valuation, self.momentum)
            if component is not None
        )

    @property
    def missing_metrics(self) -> tuple[str, ...]:
        """Every metric across every component that could not be scored."""
        return tuple(name for component in self.components for name in component.missing_metrics)

    @property
    def risk_penalty(self) -> float | None:
        """The total risk penalty, or None when risk was not assessed."""
        return self.risk.total_penalty if self.risk is not None else None

    @property
    def risk_level(self) -> RiskLevel | None:
        """The risk label, or None when risk was not assessed."""
        return self.risk.level if self.risk is not None else None
