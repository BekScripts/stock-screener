"""The market-risk inputs the advanced engine consumes, and nothing it computes.

P1.1 makes the portfolio authority aware of volatility, liquidity, covariance and
drawdown. It does not *measure* any of them. Forecasting volatility and estimating
a covariance matrix are separate problems with their own evidence and their own
failure modes, and folding them into the risk engine would make the engine
untestable and the forecasts unauditable. So the engine consumes declared values,
each carrying the moment it describes and the fingerprint of whatever produced it.

Three rules shape these types.

**Missing is a state, not a number.** An unknown volatility is not zero, an
unknown ADV is not infinite, and an absent covariance entry is not an absent
correlation. Each would quietly convert ignorance into permission, which is the
direction that costs money. `MetricState` makes the three cases distinct and the
policy decides what each one permits.

**A metric cannot describe the future.** Every metric carries `metric_as_of`, and
a metric dated after the decision is refused outright. A risk input that postdates
the decision it constrains is the leakage problem the research track spent five
phases on, arriving in a new place.

**Units are frozen.** Volatility is an annualised decimal — `0.25` means 25% — and
ADV is US dollars per session. Both are validated, because `25` meaning 25% and
`25` meaning 2500% differ by a factor of a hundred and nothing downstream could
tell which was intended.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .fingerprint import digest
from .linalg import LDL_METHOD_ID, is_positive_semidefinite

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import SecurityIdentity

__all__ = [
    "COVARIANCE_TOLERANCE",
    "LDL_METHOD_ID",
    "MAX_PLAUSIBLE_VOLATILITY",
    "CovarianceSnapshot",
    "MetricState",
    "PortfolioRiskSnapshot",
    "RiskInputError",
    "SecurityRiskMetrics",
]

#: How far a covariance matrix may depart from symmetry before it is refused.
#: Generous enough for the rounding a real estimator produces, tight enough that a
#: transposition error cannot hide inside it.
COVARIANCE_TOLERANCE = 1e-9

#: An annualised volatility above this is almost certainly a unit error — someone
#: passing 25 for 25%. A genuine 1000%-vol security exists in principle; refusing
#: it and making the caller say so explicitly is the cheaper mistake.
MAX_PLAUSIBLE_VOLATILITY = 10.0


class RiskInputError(ValueError):
    """Raised when a risk input could not describe a real market."""

    def __init__(self, code: str, detail: str) -> None:
        """Carry the machine-readable code alongside the prose.

        Args:
            code: The stable reason code.
            detail: What went wrong.
        """
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class MetricState(StrEnum):
    """Whether a risk metric is available, and if not, why not.

    The distinction is load-bearing. `UNKNOWN` means nobody measured it, which is
    a reason to be cautious. `NOT_APPLICABLE` means the question does not apply to
    this security, which is not. Collapsing them into a missing value forces the
    policy to treat caution and irrelevance identically.
    """

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class SecurityRiskMetrics:
    """One security's declared risk and liquidity metrics.

    Attributes:
        identity: The security, by durable key.
        metric_as_of: The moment these describe. Must not postdate the decision.
        source_fingerprint: The digest of whatever produced them.
        annualized_volatility: Annualised standard deviation as a decimal, so
            `0.25` is 25%. None with a `KNOWN` state is a contradiction and is
            refused.
        volatility_state: Whether the volatility is known.
        average_daily_dollar_volume: Mean traded value per session, in US dollars.
            Never share volume — the two differ by the price.
        liquidity_state: Whether the ADV is known.
        price: The mark used to convert notional to weight, when supplied.
    """

    identity: SecurityIdentity
    metric_as_of: str
    source_fingerprint: str
    annualized_volatility: float | None = None
    volatility_state: MetricState = MetricState.UNKNOWN
    average_daily_dollar_volume: float | None = None
    liquidity_state: MetricState = MetricState.UNKNOWN
    price: float | None = None

    def __post_init__(self) -> None:
        """Refuse metrics that contradict themselves or their declared units.

        Raises:
            RiskInputError: On a missing fingerprint, a state that disagrees with
                the value present, a negative or non-finite figure, or a
                volatility outside the plausible range for its declared unit.
        """
        if not self.source_fingerprint:
            raise RiskInputError(
                "RISK_METRIC_UNATTRIBUTED",
                f"{self.identity.label}: a risk metric must carry a source "
                "fingerprint. A number nothing can be traced back to cannot be "
                "audited after it has constrained a position.",
            )
        self._check(
            "annualized_volatility",
            self.annualized_volatility,
            self.volatility_state,
            upper=MAX_PLAUSIBLE_VOLATILITY,
        )
        self._check(
            "average_daily_dollar_volume",
            self.average_daily_dollar_volume,
            self.liquidity_state,
        )
        if self.price is not None and (not math.isfinite(self.price) or self.price <= 0):
            raise RiskInputError(
                "RISK_METRIC_INVALID", f"{self.identity.label}: price must be positive"
            )

    def _check(
        self, name: str, value: float | None, state: MetricState, *, upper: float | None = None
    ) -> None:
        """Refuse a value that disagrees with its state or its unit."""
        if state is MetricState.KNOWN:
            if value is None:
                raise RiskInputError(
                    "RISK_METRIC_INVALID",
                    f"{self.identity.label}: {name} is declared KNOWN with no value",
                )
            if not math.isfinite(value) or value < 0:
                raise RiskInputError(
                    "RISK_METRIC_INVALID",
                    f"{self.identity.label}: {name} must be finite and non-negative, got {value!r}",
                )
            if upper is not None and value > upper:
                raise RiskInputError(
                    "RISK_METRIC_UNIT_SUSPECT",
                    f"{self.identity.label}: {name} is {value!r}, above the plausible "
                    f"ceiling {upper}. Volatility is an annualised decimal, so 25% is "
                    "0.25 and not 25; if this really is a "
                    f"{value * 100:.0f}% security, say so by raising the ceiling "
                    "deliberately rather than by passing an ambiguous number.",
                )
        elif value is not None:
            raise RiskInputError(
                "RISK_METRIC_INVALID",
                f"{self.identity.label}: {name} carries a value while declared "
                f"{state}. A measured number and an unmeasured one must not be "
                "indistinguishable.",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the metrics as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "metric_as_of": self.metric_as_of,
            "source_fingerprint": self.source_fingerprint,
            "annualized_volatility": self.annualized_volatility,
            "volatility_state": str(self.volatility_state),
            "average_daily_dollar_volume": self.average_daily_dollar_volume,
            "liquidity_state": str(self.liquidity_state),
            "price": self.price,
            "volatility_unit": "annualised decimal (0.25 == 25%)",
            "liquidity_unit": "USD per market session",
        }


@dataclass(frozen=True)
class CovarianceSnapshot:
    """An annualised covariance matrix over an explicitly ordered identity list.

    The ordering is part of the value. A matrix whose rows correspond to whatever
    order a dictionary happened to yield is a matrix that means something different
    on the next run, so `identities` is the canonical order and the matrix is
    indexed by it.

    Attributes:
        identities: The securities, in the order the matrix is indexed by.
        matrix: Row-major annualised covariances. `matrix[i][j]` is the covariance
            of `identities[i]` and `identities[j]`.
        as_of: The moment this describes.
        annualization: How the figures were annualised, recorded because a daily
            and an annualised matrix differ by ~252 and nothing in the numbers says
            which this is.
        source_fingerprint: The digest of whatever produced it.
        _psd: The semidefiniteness verdict, established at construction. Not part
            of the value, so it stays out of equality and the fingerprint.
    """

    identities: tuple[SecurityIdentity, ...]
    matrix: tuple[tuple[float, ...], ...]
    as_of: str
    annualization: str = "252 sessions per year"
    source_fingerprint: str = ""
    _psd: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Refuse a matrix that could not be a covariance matrix.

        Nothing is repaired. An asymmetric matrix is not silently averaged and a
        negative variance is not clamped, because both are symptoms of a bug in
        whatever produced them and hiding the symptom keeps the bug.

        Raises:
            RiskInputError: If the matrix is not square, not symmetric within
                `COVARIANCE_TOLERANCE`, not finite, has a negative diagonal entry,
                or repeats an identity.
        """
        size = len(self.identities)
        keys = [identity.key for identity in self.identities]
        if len(set(keys)) != size:
            raise RiskInputError(
                "COVARIANCE_IDENTITY_REPEATED",
                "an identity appears more than once in the covariance ordering",
            )
        if len(self.matrix) != size or any(len(row) != size for row in self.matrix):
            raise RiskInputError(
                "COVARIANCE_NOT_SQUARE",
                f"a {size}-identity covariance needs a {size}x{size} matrix, got "
                f"{len(self.matrix)} rows of lengths "
                f"{sorted({len(row) for row in self.matrix})}",
            )
        for row_index, row in enumerate(self.matrix):
            for column_index, value in enumerate(row):
                if not math.isfinite(value):
                    raise RiskInputError(
                        "COVARIANCE_NOT_FINITE",
                        f"entry [{row_index}][{column_index}] is {value!r}",
                    )
                mirrored = self.matrix[column_index][row_index]
                if abs(value - mirrored) > COVARIANCE_TOLERANCE:
                    raise RiskInputError(
                        "COVARIANCE_NOT_SYMMETRIC",
                        f"entry [{row_index}][{column_index}] is {value!r} but "
                        f"[{column_index}][{row_index}] is {mirrored!r}. The matrix is "
                        "not averaged into symmetry: the asymmetry is evidence of a "
                        "fault in whatever produced it.",
                    )
            if row[row_index] < -COVARIANCE_TOLERANCE:
                raise RiskInputError(
                    "COVARIANCE_NEGATIVE_VARIANCE",
                    f"the variance of {self.identities[row_index].label} is "
                    f"{row[row_index]!r}; a variance cannot be negative",
                )
        # Symmetry, finiteness and a non-negative diagonal do not prove positive
        # semidefiniteness, and an indefinite matrix means some portfolio has
        # negative variance. Validating here rather than when a particular target
        # happens to expose it: an invalid covariance is invalid regardless of
        # which weights it is asked about.
        verdict = is_positive_semidefinite(self.matrix, tolerance=COVARIANCE_TOLERANCE)
        if not verdict.semidefinite:
            where = (
                self.identities[verdict.failure_index].label
                if verdict.failure_index is not None
                else "unknown"
            )
            raise RiskInputError(
                "COVARIANCE_INDEFINITE",
                f"the matrix is not positive semidefinite ({verdict.failure} at "
                f"{where}, minimum pivot {verdict.minimum_pivot:.6g}). Some portfolio "
                "therefore has negative variance under it. The matrix is not projected "
                "onto the semidefinite cone: an indefinite covariance is a fault in "
                "whatever estimated it, and repairing it here would hide the fault "
                "behind a number that looks reasonable.",
            )
        object.__setattr__(self, "_psd", verdict)

    @property
    def index(self) -> dict[tuple[str, int], int]:
        """Row position per durable identity key."""
        return {identity.key: position for position, identity in enumerate(self.identities)}

    def covers(self, keys: Sequence[tuple[str, int]]) -> bool:
        """Whether every required identity has a row.

        Args:
            keys: The durable keys the portfolio needs.

        Returns:
            True when all of them are present. Extra identities are fine: a matrix
            estimated over a wider universe is subselected deterministically.
        """
        return set(keys) <= set(self.index)

    def missing(self, keys: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
        """Which required identities have no row, in identity order.

        Args:
            keys: The durable keys the portfolio needs.

        Returns:
            The absent keys. Never treated as zero covariance.
        """
        return sorted(set(keys) - set(self.index))

    def variance_of(self, weights: dict[tuple[str, int], float]) -> float:
        """The portfolio variance `wᵀΣw` over the supplied weights.

        Cash contributes nothing: this is a market-risk model, and holding cash is
        modelled as holding no market exposure. That understates risk in a currency
        crisis and overstates safety for a leveraged book, neither of which this
        baseline claims to cover.

        Args:
            weights: `{identity key: weight}`. Keys absent from the matrix are an
                error rather than a zero.

        Returns:
            The annualised variance. A residue inside `COVARIANCE_TOLERANCE` is
            normalised to zero, which is floating-point noise rather than
            information.

        Raises:
            RiskInputError: If a weighted identity has no row, or if the quadratic
                form is materially negative. P1.1 clamped that to zero and reported
                a volatility of zero, which is the worst available way to be wrong
                about risk because it reports perfect safety.
        """
        absent = self.missing(list(weights))
        if absent:
            raise RiskInputError(
                "COVARIANCE_IDENTITY_MISSING",
                f"{[f'{kind}:{number}' for kind, number in absent]} have weight but no "
                "covariance row. A missing row is not a zero covariance: assuming it "
                "would report a diversified portfolio that may not be one.",
            )
        index = self.index
        total = 0.0
        for left, left_weight in sorted(weights.items()):
            row = self.matrix[index[left]]
            for right, right_weight in sorted(weights.items()):
                total += left_weight * right_weight * row[index[right]]
        if total < -COVARIANCE_TOLERANCE:
            raise RiskInputError(
                "COVARIANCE_INDEFINITE",
                f"the portfolio variance is {total!r} under this covariance. A "
                "negative variance is never clamped to zero: reporting zero "
                "volatility for a portfolio whose risk the matrix cannot describe "
                "is the one failure mode that looks like perfect safety.",
            )
        return max(total, 0.0)

    def volatility_of(self, weights: dict[tuple[str, int], float]) -> float:
        """The portfolio volatility `sqrt(wᵀΣw)`.

        Args:
            weights: `{identity key: weight}`.

        Returns:
            The annualised standard deviation.
        """
        return quantise(math.sqrt(self.variance_of(weights)))

    def correlation_diagnostics(self, keys: Sequence[tuple[str, int]]) -> dict[str, float | None]:
        """Average and maximum pairwise correlation over the supplied identities.

        Diagnostics only. P1.1 defines no correlation limit, because a
        concentration rule needs its own design and its own evidence.

        Args:
            keys: The identities to summarise.

        Returns:
            The average and maximum off-diagonal correlation, or None where a
            variance is zero and the correlation is undefined rather than zero.
        """
        index = self.index
        present = [key for key in sorted(set(keys)) if key in index]
        pairs: list[float] = []
        undefined = 0
        for position, left in enumerate(present):
            for right in present[position + 1 :]:
                left_var = self.matrix[index[left]][index[left]]
                right_var = self.matrix[index[right]][index[right]]
                if left_var <= 0 or right_var <= 0:
                    undefined += 1
                    continue
                pairs.append(
                    self.matrix[index[left]][index[right]] / math.sqrt(left_var * right_var)
                )
        return {
            "pairs": float(len(pairs)),
            "undefined_pairs": float(undefined),
            "average_pairwise_correlation": (quantise(sum(pairs) / len(pairs)) if pairs else None),
            "maximum_pairwise_correlation": quantise(max(pairs)) if pairs else None,
        }

    def component_variance(self, weights: dict[tuple[str, int], float]) -> dict[str, float]:
        """Each identity's component contribution to portfolio variance.

        `wᵢ · (Σw)ᵢ`, which sums exactly to `wᵀΣw`. Diagnostic only: component
        risk is not used for sizing in P1.1, because a risk-budgeting rule is a
        separate design decision.

        Args:
            weights: `{identity key: weight}`.

        Returns:
            `{identity label: contribution}`, summing to the portfolio variance.
        """
        index = self.index
        contributions: dict[str, float] = {}
        for key, weight in sorted(weights.items()):
            row = self.matrix[index[key]]
            marginal = sum(
                other_weight * row[index[other]] for other, other_weight in sorted(weights.items())
            )
            contributions[f"{key[0]}:{key[1]}"] = quantise(weight * marginal)
        return contributions

    @property
    def psd(self) -> Any:
        """The semidefiniteness verdict established at construction."""
        return self._psd

    def body(self) -> dict[str, Any]:
        """The fingerprinted body, in the canonical identity order."""
        return {
            "as_of": self.as_of,
            "annualization": self.annualization,
            "source_fingerprint": self.source_fingerprint,
            "identities": [identity.label for identity in self.identities],
            "matrix": [[quantise(value) for value in row] for row in self.matrix],
        }

    def fingerprint(self) -> str:
        """The matrix's deterministic digest.

        Changing one entry, one identity, the as-of or the annualisation convention
        all change it.
        """
        return digest(self.body())


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    """Everything the advanced risk engine knows about the market and the book.

    Attributes:
        as_of: The moment this describes.
        portfolio_nav: Net asset value now.
        portfolio_peak_nav: The highest NAV reached, for the drawdown.
        metrics: Per-security risk and liquidity metrics, keyed by durable identity.
        covariance: The covariance snapshot, when one is available. None means no
            portfolio-volatility constraint can be evaluated, which the policy
            must then decide how to treat.
        source_fingerprint: The digest of whatever assembled this.
    """

    as_of: str
    portfolio_nav: float
    portfolio_peak_nav: float
    metrics: tuple[SecurityRiskMetrics, ...] = ()
    covariance: CovarianceSnapshot | None = None
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Refuse a snapshot that could not describe a real book.

        Raises:
            RiskInputError: On a non-positive NAV, a peak below the current NAV, or
                a repeated identity.
        """
        for name in ("portfolio_nav", "portfolio_peak_nav"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise RiskInputError(
                    "RISK_SNAPSHOT_INVALID", f"{name} must be positive and finite, got {value!r}"
                )
        if self.portfolio_peak_nav < self.portfolio_nav - EXPOSURE_TOLERANCE:
            raise RiskInputError(
                "RISK_SNAPSHOT_INVALID",
                f"peak NAV {self.portfolio_peak_nav!r} is below current NAV "
                f"{self.portfolio_nav!r}. A peak that is not a peak would report a "
                "positive drawdown and could only loosen a limit.",
            )
        keys = [row.identity.key for row in self.metrics]
        if len(set(keys)) != len(keys):
            raise RiskInputError(
                "RISK_SNAPSHOT_INVALID", "an identity has more than one metric row"
            )

    @property
    def by_identity(self) -> dict[tuple[str, int], SecurityRiskMetrics]:
        """Metrics keyed by durable identity."""
        return {row.identity.key: row for row in self.metrics}

    @property
    def drawdown(self) -> float:
        """Current drawdown as a non-positive decimal: `nav / peak - 1`."""
        return quantise(self.portfolio_nav / self.portfolio_peak_nav - 1.0)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body, in identity order."""
        return {
            "as_of": self.as_of,
            "portfolio_nav": quantise(self.portfolio_nav),
            "portfolio_peak_nav": quantise(self.portfolio_peak_nav),
            "drawdown": self.drawdown,
            "source_fingerprint": self.source_fingerprint,
            "metrics": [row.as_json() for row in sorted(self.metrics, key=lambda m: m.identity)],
            "covariance_fingerprint": (
                self.covariance.fingerprint() if self.covariance is not None else None
            ),
        }

    def fingerprint(self) -> str:
        """The snapshot's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}
