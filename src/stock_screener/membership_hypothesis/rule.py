"""`ONE_DECILE_HYSTERESIS_V1` — the one membership rule H2 proposes.

R1.3 established that per-rebalance turnover tracks one minus retention, and
that entering and exiting a position are both forced by the target set changing.
Reducing turnover further therefore means changing how the selected set responds
to a drifting ranking, which is what this rule does and the only thing it does.

The rule adds exactly one idea to R1.2: a position already held survives a
smaller rank decline than would be required to buy it. Everything else — the
cadence, the score, the target count, the weighting, the execution timing — is
R1.2's, unchanged.

Two choices here are deliberately structural rather than chosen, because a
number picked from R1.3's distributions would be a parameter fitted to the
result it must later be judged against:

- the **target count** is not recomputed. It is whatever the frozen R1.2 rule
  produces for that cross-section, so the two strategies always hold the same
  number of names;
- the **retention band** is two selection widths, not a separately chosen
  percentile. One width admits; a second width retains. That is what makes it
  "one decile of hysteresis" rather than a tuned value that happens to be near
  twenty percent.

Nothing in this module reads a price, a return or a label. `select` sees rank
order and eligibility, and that is all it can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ENTRY_WIDTHS",
    "RETENTION_WIDTHS",
    "RULE",
    "RULE_VERSION",
    "Candidate",
    "Selection",
    "rank",
    "retention_limit",
    "select",
]

#: The rule this hypothesis freezes. One version, one parameterisation.
RULE_VERSION = "ONE_DECILE_HYSTERESIS_V1"

#: Selection widths a name must be inside to be *bought*. One, by definition of
#: the top decile.
ENTRY_WIDTHS = 1

#: Selection widths a name already held must be inside to be *kept*. Two — the
#: entry width plus one more, which is the whole of the hypothesis.
RETENTION_WIDTHS = 2

#: The frozen rule, stated so a later phase can assert it verbatim rather than
#: restate it.
RULE: dict[str, str] = {
    "rule_version": RULE_VERSION,
    "signal": "quarter-end close — the last benchmark session of each calendar quarter",
    "ranking": "frozen COMPOUNDER_V1_2, descending; ties by (kind, identity_id)",
    "entry": (
        "an identity not currently held enters only inside the ordinary top-decile "
        "target count, which is exactly R1.2's selection"
    ),
    "retention": (
        "an identity currently held is kept while its rank position is inside twice "
        "the target count — one additional selection width beyond the entry "
        "threshold, which is the top 20% to the same rounding as the entry rule"
    ),
    "target_size": (
        "the ordinary top-decile target count for that cross-section, taken from "
        "the frozen R1.2 rule and never recomputed"
    ),
    "over_capacity": "the best-ranked retained incumbents are kept, worst-ranked dropped",
    "vacancy_fill": "the highest-ranked eligible identities not already held",
    "forced_exit": (
        "an incumbent that is ineligible, unscored, untradable or terminal under the "
        "frozen contract is removed before the band is applied; being held is never a "
        "reason to keep it"
    ),
    "weighting": "equal weight",
    "execution": "open of the first benchmark session strictly after the signal",
    "rebalance": "calendar quarter ends only",
    "first_rebalance": "no incumbents exist, so it is exactly R1.2's top-decile selection",
    "sector_logic": "NONE",
    "leverage": "NONE",
    "shorting": "NONE",
    "minimum_holding_period": "NONE",
    "cooldown": "NONE",
    "turnover_penalty": "NONE",
    "score_smoothing": "NONE",
    "cost_sensitivity": "SIMPLE_10BPS_PER_SIDE_V1, unchanged",
    "terminal_policy": "TERMINAL_LEG_POLICY_V1, unchanged",
    "share_policy": "FRACTIONAL_SHARES_RESEARCH_V1, unchanged",
    "benchmark": "SPY, unchanged",
}


@dataclass(frozen=True)
class Candidate:
    """One identity in a ranked cross-section, carrying no outcome.

    The type exists to make the rule's inputs auditable: there is no field here
    a forward return could arrive in.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`.
        identity_id: The identity's id within its kind.
        final_score: The frozen `COMPOUNDER_V1_2` score at the decision.
        eligible: Whether the frozen contract permits holding it at this
            decision — executable, scored, tradable and not terminal.
    """

    kind: str
    identity_id: int
    final_score: float
    eligible: bool = True

    @property
    def key(self) -> tuple[str, int]:
        """The identity, as the harness addresses it."""
        return (self.kind, self.identity_id)


@dataclass(frozen=True)
class Selection:
    """What the rule chose, and why each name is in it.

    Attributes:
        selected: The chosen identities, best-ranked first.
        target_count: The ordinary top-decile count this had to match.
        retained_in_band: Incumbents kept that the ordinary rule would have sold.
        retained_in_decile: Incumbents kept that the ordinary rule would also hold.
        admitted: Identities bought that were not held before.
        forced_out: Incumbents removed because the contract no longer permits them.
        dropped_out_of_band: Incumbents sold because they fell past the band.
        dropped_over_capacity: Incumbents in the band sold to meet the target count.
        displaced: `{incumbent: displaced candidate}` — the higher-ranked name each
            band-retained incumbent kept out.
    """

    selected: tuple[tuple[str, int], ...]
    target_count: int
    retained_in_band: tuple[tuple[str, int], ...] = ()
    retained_in_decile: tuple[tuple[str, int], ...] = ()
    admitted: tuple[tuple[str, int], ...] = ()
    forced_out: tuple[tuple[str, int], ...] = ()
    dropped_out_of_band: tuple[tuple[str, int], ...] = ()
    dropped_over_capacity: tuple[tuple[str, int], ...] = ()
    displaced: tuple[tuple[tuple[str, int], tuple[str, int]], ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the selection as a JSON-safe mapping."""
        return {
            "target_count": self.target_count,
            "selected": [list(key) for key in self.selected],
            "retained_in_band": [list(key) for key in self.retained_in_band],
            "retained_in_decile": [list(key) for key in self.retained_in_decile],
            "admitted": [list(key) for key in self.admitted],
            "forced_out": [list(key) for key in self.forced_out],
            "dropped_out_of_band": [list(key) for key in self.dropped_out_of_band],
            "dropped_over_capacity": [list(key) for key in self.dropped_over_capacity],
            "displaced": [
                {"retained": list(held), "displaced": list(other)} for held, other in self.displaced
            ],
        }


def rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Order a cross-section the way the frozen rules describe.

    Descending score, ties broken by `(kind, identity_id)` ascending — the
    ordering `EXPECTED_RULES` states and R1.3 measured percentiles under.

    Args:
        candidates: The cross-section, in any order.

    Returns:
        The same candidates, best-ranked first.
    """
    return sorted(candidates, key=lambda row: (-row.final_score, row.kind, row.identity_id))


def retention_limit(target_count: int) -> int:
    """Return the rank position an incumbent must stay inside to be kept.

    Args:
        target_count: The ordinary top-decile count for this cross-section.

    Returns:
        `RETENTION_WIDTHS * target_count` — the band, in names rather than
        percent, so it inherits the entry rule's rounding rather than adding a
        second one.
    """
    return RETENTION_WIDTHS * target_count


def select(
    candidates: Sequence[Candidate],
    incumbents: Iterable[tuple[str, int]],
    target_count: int,
) -> Selection:
    """Apply `ONE_DECILE_HYSTERESIS_V1` to one cross-section.

    The order is the one the specification fixes: rank, drop incumbents the
    contract no longer permits, keep those still inside the band, trim to the
    target count worst-first, then fill any vacancy with the best-ranked
    identities not already held.

    Args:
        candidates: The cross-section at this decision. Ineligible rows may be
            present; they are never selected and never retained.
        incumbents: Identities held going into this decision. Empty at the first
            rebalance, which is what makes that one ordinary top-decile.
        target_count: The ordinary top-decile count, from the frozen R1.2 rule.

    Returns:
        The selection, with each name attributed to the step that placed it.

    Raises:
        ValueError: If `target_count` is negative.
    """
    if target_count < 0:
        raise ValueError(f"target_count must not be negative, got {target_count}")

    ordered = rank(candidates)
    positions = {row.key: index for index, row in enumerate(ordered)}
    permitted = {row.key: row.eligible for row in ordered}
    held = list(dict.fromkeys(incumbents))
    limit = retention_limit(target_count)

    forced_out: list[tuple[str, int]] = []
    survivors: list[tuple[str, int]] = []
    out_of_band: list[tuple[str, int]] = []
    for key in held:
        if not permitted.get(key, False):
            forced_out.append(key)
        elif positions[key] < limit:
            survivors.append(key)
        else:
            out_of_band.append(key)
    survivors.sort(key=lambda key: positions[key])

    dropped_over_capacity: list[tuple[str, int]] = []
    if len(survivors) > target_count:
        dropped_over_capacity = survivors[target_count:]
        survivors = survivors[:target_count]

    chosen = list(survivors)
    admitted: list[tuple[str, int]] = []
    if len(chosen) < target_count:
        already = set(chosen)
        for row in ordered:
            if len(chosen) == target_count:
                break
            if row.key in already or not row.eligible:
                continue
            chosen.append(row.key)
            admitted.append(row.key)
    chosen.sort(key=lambda key: positions[key])

    ordinary: list[tuple[str, int]] = []
    for row in ordered:
        if len(ordinary) == target_count:
            break
        if row.eligible:
            ordinary.append(row.key)
    inside_decile = set(ordinary)
    retained_in_decile = tuple(key for key in survivors if key in inside_decile)
    retained_in_band = tuple(key for key in survivors if key not in inside_decile)

    selected = set(chosen)
    passed_over = [key for key in ordinary if key not in selected]
    displaced = tuple(zip(retained_in_band, passed_over, strict=False))

    return Selection(
        selected=tuple(chosen),
        target_count=target_count,
        retained_in_band=retained_in_band,
        retained_in_decile=retained_in_decile,
        admitted=tuple(admitted),
        forced_out=tuple(forced_out),
        dropped_out_of_band=tuple(out_of_band),
        dropped_over_capacity=tuple(dropped_over_capacity),
        displaced=displaced,
    )
