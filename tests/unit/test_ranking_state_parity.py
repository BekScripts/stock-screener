"""The two spellings of PRELIMINARY and FINAL must stay the same two strings.

`research.RankingState` mirrors the `PRELIMINARY` / `FINAL` constants that
`data-access` writes to `score_snapshots.ranking_state`. The duplication is
deliberate — the research contract stays independent of persistence, so it can be
tested against a hand-written brief with no database — but a mirror nobody checks
is a mirror that drifts.

Drift would not raise. `RankingState("PRELIM")` would fail loudly, but a value
renamed on one side and not the other turns every brief's ranking state into a
silent fallback, and the confidence ceiling would quietly stop distinguishing an
enriched company from an unverified one.

This test is the thing that fails instead. It is the reason ownership of these
two strings has not been refactored yet: a check this cheap buys time to decide
where they should live.
"""

from __future__ import annotations

import pytest

from data_access import FINAL, PRELIMINARY
from research import RankingState


@pytest.mark.unit
def test_preliminary_matches_the_persistence_layer() -> None:
    assert RankingState.PRELIMINARY.value == PRELIMINARY


@pytest.mark.unit
def test_final_matches_the_persistence_layer() -> None:
    assert RankingState.FINAL.value == FINAL


@pytest.mark.unit
def test_neither_side_has_gained_a_state_the_other_lacks() -> None:
    assert {state.value for state in RankingState} == {PRELIMINARY, FINAL}
