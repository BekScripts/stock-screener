"""`ALPHATRADER_COMPOUNDER_MONTHLY_V2` — V1, deployable where it is routable.

V2 exists because of a mismatch between what V1 wants and what the paper venue
can do. V1 expresses exposure as a weight; the risk engine turns a weight into a
quantity that is almost never a whole number; and the venue will only accept a
fractional quantity for instruments it states are fractionable. For the rest, the
authorised exposure cannot be placed as authorised.

**The restriction lives here, in strategy authority, and not underneath it.**
That placement is the whole design. Narrowing the universe inside execution
would mean the strategy selected a name, the risk engine authorised exposure to
it, and then something further down quietly dropped or rounded the order — the
decision record would claim a position the account never held. Doing it here
means the decision itself never asks for what cannot be placed, and the record
says, per company, that the reason was the venue and not the research.

What changes from V1, exhaustively:

- Selection ranks the **intersection** of V1's eligibility and the deployment
  universe. The top-decile count is computed *after* that intersection, so the
  decile is a decile of what can actually be held, not of a larger set that was
  then trimmed.
- A company that passes Compounder eligibility and fails only deployment is
  recorded as `DEPLOYMENT_NOT_FRACTIONALLY_ROUTABLE`. It is never folded into a
  research exclusion reason: the company is eligible, the *venue* is the
  constraint, and a record saying otherwise would be false about the company.
- The decision binds the deployment universe's fingerprint, so a decision made
  against one set of venue capabilities can never be read as though it had been
  made against another.

Everything else is V1 unchanged, and by construction rather than by assertion:
the ranking, the `ceil(N/10)` rule, the equal `1/K` weighting, long-only, no
hysteresis, no overlay, the monthly cadence, the close-of-session decision
instant, the first-execution-session rule, and the refusal to size, read cash,
read broker state or emit an order. V2 inherits all of it and overrides only the
universe it ranks over.

What V2 still never does: widen a universe to reach a target count, substitute a
different company for an excluded one, round a quantity, or treat a venue
capability as a judgement about a business.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .compounder_monthly import CompounderMonthlyStrategy
from .errors import Code, StrategyError

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

    from .models import StrategyDecision, StrategyInputBundle, StrategyInputRecord
    from .state import StrategyState

__all__ = [
    "DEPLOYMENT_EXCLUSION_REASON",
    "STRATEGY_VERSION_V2",
    "CompounderMonthlyFractionableStrategy",
    "DeploymentUniverseView",
]

#: V2's frozen version. A new version rather than an edit to V1: V2 can select a
#: different set of companies from the same evidence, so comparing the two as
#: though one formula had produced both would be comparing across versions.
STRATEGY_VERSION_V2 = "ALPHATRADER_COMPOUNDER_MONTHLY_V2"

#: Why a research-eligible company was not selected. Distinct from every
#: research reason on purpose: this company qualified, and what stopped it was
#: the venue's whole-share-only or absent fractionability.
DEPLOYMENT_EXCLUSION_REASON = "DEPLOYMENT_NOT_FRACTIONALLY_ROUTABLE"


@runtime_checkable
class DeploymentUniverseView(Protocol):
    """What V2 needs from a deployment universe, and nothing more.

    A protocol rather than a concrete import so the strategy layer never depends
    on the paper-runtime layer: dependencies run one way, from runtime to
    strategy. It also keeps the strategy unable to reach anything else on the
    universe — it may ask whether an identity is deployable and what evidence it
    came from, and it cannot enumerate, extend or re-derive membership.
    """

    def contains(self, identity: Any) -> bool:
        """Whether this identity may be deployed to."""
        ...

    def fingerprint(self) -> str:
        """The digest of the universe's membership and its evidence."""
        ...


class CompounderMonthlyFractionableStrategy(CompounderMonthlyStrategy):
    """V1's selection, over the identities the paper venue can actually take.

    Subclasses V1 rather than reimplementing it. That is deliberate: a second
    copy of the ranking, the rounding rule and the weighting would be a second
    thing to keep in step, and the first time the two drifted the system would
    hold a portfolio no single version had authorised.
    """

    strategy_version = STRATEGY_VERSION_V2

    def __init__(self, universe: DeploymentUniverseView) -> None:
        """Bind the strategy to one deployment universe.

        Args:
            universe: The identities this deployment may route fractional
                quantities to. Required — there is no default and no
                unrestricted construction, because a V2 that silently fell back
                to V1's universe would be V1 wearing V2's version.
        """
        self.universe = universe

    def policy(self) -> dict[str, Any]:
        """Return the frozen policy: V1's, plus what V2 changes.

        Built from V1's policy rather than retyped, so every rule V2 does not
        change is V2's rule by construction. The three keys below are the only
        differences, and the fingerprint over this dict is what a deployment
        grant is bound to.

        Returns:
            The policy.
        """
        policy = super().policy()
        policy.update(
            {
                "strategy_version": STRATEGY_VERSION_V2,
                "deployment_universe": (
                    "ALPHATRADER_PAPER_FRACTIONABLE_UNIVERSE_V1 — routable and "
                    "provider-stated fractionable, derived from the instrument "
                    "mapping and never authored"
                ),
                "selection_population": (
                    "intersection of domain.scores.is_rank_eligible and the "
                    "deployment universe; the top-decile count is computed after "
                    "the intersection"
                ),
                "deployment_exclusion_reason": DEPLOYMENT_EXCLUSION_REASON,
                "deployment_restriction_placement": (
                    "STRATEGY_AUTHORITY — not execution, not order sizing, not quantity rounding"
                ),
                "sizing_authority": ("PORTFOLIO_RISK_ENGINE_V1_3 — this strategy sizes nothing"),
            }
        )
        return policy

    def decide(
        self,
        bundle: StrategyInputBundle,
        *,
        session: str,
        calendar: ExecutionCalendar,
        previous: StrategyState | None = None,
    ) -> StrategyDecision:
        """Decide over the deployable intersection, or refuse.

        The order matters. Admissibility is checked against the **whole** bundle
        before anything is narrowed, so a stale record still refuses the
        decision exactly as it does under V1 — narrowing the universe must not
        become a way to drop the evidence that would have stopped the decision.

        Args:
            bundle: The research evidence.
            session: The market session to decide on.
            calendar: The market calendar. Authoritative.
            previous: The last decision's state, for computing exits.

        Returns:
            The decision. `eligible_count` is the size of the intersection —
            what the decile was actually taken from — while `considered_count`
            remains everything offered, and `excluded_counts` carries both the
            research reasons and `DEPLOYMENT_NOT_FRACTIONALLY_ROUTABLE`.

        Raises:
            StrategyError: Everything V1 raises, unchanged, and
                `STRATEGY_SESSION_NOT_IN_CALENDAR` /
                `STRATEGY_NO_DECISION_DUE` before any narrowing happens.
        """
        from .compounder_monthly import is_decision_session

        # V1's refusals first, against the full bundle: a session that is not a
        # month end is not a decision at all, and checking admissibility or
        # membership before that would do work for a decision nobody asked for.
        if session not in calendar.sessions:
            raise StrategyError(
                Code.STRATEGY_SESSION_NOT_IN_CALENDAR,
                f"{session} is not a session in calendar {calendar.name!r}; month ends "
                "are read from the calendar, never computed from weekdays",
            )
        if not is_decision_session(session, calendar=calendar):
            raise StrategyError(
                Code.STRATEGY_NO_DECISION_DUE,
                f"{session} is not the last session of its month, so this strategy "
                "makes no selection. The existing desired exposure stands; nothing "
                "is re-ranked and nothing is liquidated",
            )
        bundle.require_admissible(decision_at=self.decision_instant_for(session))

        kept, excluded = self._partition(bundle.ordered)
        narrowed = replace(bundle, records=kept)
        decision = super().decide(narrowed, session=session, calendar=calendar, previous=previous)
        counts = dict(decision.excluded_counts)
        if excluded:
            counts[DEPLOYMENT_EXCLUSION_REASON] = len(excluded)
        return replace(
            decision,
            # Restored from the full bundle: the deployable intersection is what
            # was ranked, but what was *offered* is a fact about the evidence
            # and does not shrink because the venue cannot route some of it.
            considered_count=len(bundle.records),
            excluded_counts=counts,
            deployment_universe_fingerprint=self.universe.fingerprint(),
        )

    def _partition(
        self, records: tuple[StrategyInputRecord, ...]
    ) -> tuple[tuple[StrategyInputRecord, ...], tuple[StrategyInputRecord, ...]]:
        """Split records into what may be ranked and what the venue cannot take.

        Only a research-selectable record can be excluded for deployment. One
        that already fails Compounder eligibility stays in the kept set — it is
        never ranked, and leaving it there is what lets V1's own exclusion
        counting continue to report its real reason instead of losing it behind
        a deployment reason it does not deserve.

        Args:
            records: Every record offered, in canonical order.

        Returns:
            `(kept, excluded_for_deployment)`.
        """
        kept: list[StrategyInputRecord] = []
        excluded: list[StrategyInputRecord] = []
        for row in records:
            if row.is_selectable and not self.universe.contains(row.identity):
                excluded.append(row)
            else:
                kept.append(row)
        return tuple(kept), tuple(excluded)

    def deployment_exclusions(self, bundle: StrategyInputBundle) -> tuple[tuple[Any, str], ...]:
        """Which eligible companies this deployment cannot route to, and why.

        For an operator reading why a company they expected is absent. Returns
        the identity rather than a symbol, and names the venue reason.

        Args:
            bundle: The research evidence.

        Returns:
            `(identity, reason)` pairs in canonical identity order. Empty when
            every eligible company is deployable.
        """
        _, excluded = self._partition(bundle.ordered)
        return tuple((row.identity, DEPLOYMENT_EXCLUSION_REASON) for row in excluded)
