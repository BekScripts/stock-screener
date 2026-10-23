"""Separating "can we fetch this filing" from "what may it be credited with".

Phase 9L.3 measured what happens when those two questions get merged: the
planner credited every filing with covering every tracked identity sharing its
CIK, predicted 6,402 month-ends for a validation batch, and delivered 2,544.
The secondary identities were baby bonds and preferred stock that never carry a
common-stock cover row.

The tests here pin the repair from both directions. A filing must not be
credited with identities the record does not show carrying common stock — and,
just as importantly, it *must* still be credited where several identities are
all genuinely common. `LSXMA`/`LSXMB`/`LSXMK` are three real Liberty tracking
classes; "distrust every multi-identity issuer" would be a different wrong
answer with the same shape.
"""

from __future__ import annotations

import pytest

from stock_screener.sec_evidence import (
    IdentityClass,
    Reach,
    build_reachability,
)

pytestmark = pytest.mark.integration

COMMON = ("COMPANY", 1)
BOND_A = ("COMPANY", 2)
BOND_B = ("COMPANY", 3)
SECOND_COMMON = ("COMPANY", 4)


class TestIssuerLevelIsNotSecurityLevel:
    """The 9L.3 defect, made structurally impossible."""

    def test_a_sole_identity_is_credited(self) -> None:
        model = build_reachability({10: [COMMON]}, [], [])

        assert model.by_cik[10].kind is Reach.SAFE_SINGLE
        assert model.credited(10) == {COMMON}

    def test_only_the_known_common_identity_is_credited(self) -> None:
        """AFG/AFGB/AFGC: one common stock, the rest baby bonds."""
        model = build_reachability(
            {10: [COMMON, BOND_A, BOND_B]},
            common_observations=[(10, COMMON)],
            other_observations=[(10, BOND_A), (10, BOND_B)],
        )

        reach = model.by_cik[10]
        assert reach.kind is Reach.SAFE_COMMON_CLASS
        assert reach.credited == {COMMON}
        assert reach.excluded_non_common == {BOND_A, BOND_B}
        assert reach.over_credit_avoided == 2

    def test_a_known_non_common_identity_gets_no_credit(self) -> None:
        model = build_reachability(
            {10: [COMMON, BOND_A]},
            common_observations=[(10, COMMON)],
            other_observations=[(10, BOND_A)],
        )

        assert BOND_A not in model.credited(10)
        assert model.identity_class[BOND_A] is IdentityClass.NON_COMMON

    def test_several_genuine_common_classes_are_all_credited(self) -> None:
        """LSXMA/LSXMB/LSXMK are three real common classes, not an error."""
        model = build_reachability(
            {10: [COMMON, SECOND_COMMON]},
            common_observations=[(10, COMMON), (10, SECOND_COMMON)],
        )

        assert model.credited(10) == {COMMON, SECOND_COMMON}
        assert model.by_cik[10].kind is Reach.SAFE_COMMON_CLASS

    def test_several_identities_none_known_common_credits_nothing(self) -> None:
        """Conservative where the record is silent — never a share of the total."""
        model = build_reachability({10: [COMMON, BOND_A, BOND_B]}, [], [])

        reach = model.by_cik[10]
        assert reach.kind is Reach.MULTI_CLASS_UNASSIGNED
        assert reach.credited == frozenset()
        assert reach.over_credit_avoided == 3

    def test_the_old_rule_would_have_credited_everything(self) -> None:
        """Reproduces the v1 over-credit, then shows the repair removing it."""
        tracked = {10: [COMMON, BOND_A, BOND_B]}
        old_credit = len(tracked[10])

        model = build_reachability(
            tracked,
            common_observations=[(10, COMMON)],
            other_observations=[(10, BOND_A), (10, BOND_B)],
        )

        assert old_credit == 3, "v1 credited every identity sharing the CIK"
        assert len(model.credited(10)) == 1
        assert model.by_cik[10].over_credit_avoided == 2

    def test_an_unsafe_sole_identity_is_not_credited(self) -> None:
        model = build_reachability({10: [COMMON]}, [], [], ambiguous=[COMMON])

        assert model.by_cik[10].kind is Reach.IDENTITY_AMBIGUOUS
        assert model.credited(10) == frozenset()

    def test_an_issuer_with_no_tracked_identity_is_not_a_candidate(self) -> None:
        model = build_reachability({10: []}, [], [])

        assert model.by_cik[10].kind is Reach.NO_CIK
        assert model.credited(10) == frozenset()

    def test_an_unknown_cik_credits_nothing_rather_than_raising(self) -> None:
        model = build_reachability({10: [COMMON]}, [(10, COMMON)], [])

        assert model.credited(999) == frozenset()

    def test_a_null_cik_identity_is_never_fabricated(self) -> None:
        """A security with no filer handle must not acquire one by association."""
        model = build_reachability(
            {10: [COMMON]}, common_observations=[(10, COMMON)], other_observations=[]
        )

        assert all(cik == 10 for cik in model.by_cik)
        assert BOND_A not in model.identity_class


class TestKnownMultiClassCases:
    """The filers Phase 9L.3 named, in the shape the record actually holds."""

    @pytest.mark.parametrize(
        ("label", "identities", "common"),
        [
            ("AIZ/AIZN", 2, 1),
            ("RILY family", 6, 1),
            ("AFG family", 5, 1),
            ("SO family", 5, 1),
            ("PRU family", 4, 1),
        ],
    )
    def test_one_common_identity_survives_among_many(
        self, label: str, identities: int, common: int
    ) -> None:
        tracked = [("COMPANY", i) for i in range(identities)]
        model = build_reachability(
            {77: tracked},
            common_observations=[(77, tracked[0])],
            other_observations=[(77, t) for t in tracked[1:]],
        )

        assert len(model.credited(77)) == common, label
        assert model.credited(77) == {tracked[0]}
        assert model.by_cik[77].over_credit_avoided == identities - common

    def test_class_counts_separate_common_from_unknown(self) -> None:
        model = build_reachability(
            {10: [COMMON, BOND_A, BOND_B]},
            common_observations=[(10, COMMON)],
            other_observations=[(10, BOND_A)],
        )

        counts = model.class_counts()
        assert counts["COMMON_ELIGIBLE"] == 1
        assert counts["NON_COMMON"] == 1
        assert counts["UNKNOWN_CLASS"] == 1


class TestModelIsDeterministic:
    def test_observation_order_does_not_change_the_result(self) -> None:
        tracked = {10: [COMMON, BOND_A, BOND_B]}
        forward = build_reachability(tracked, [(10, COMMON)], [(10, BOND_A), (10, BOND_B)])
        backward = build_reachability(tracked, [(10, COMMON)], [(10, BOND_B), (10, BOND_A)])

        assert forward.credited(10) == backward.credited(10)
        assert forward.counts() == backward.counts()

    def test_repeated_observations_do_not_change_credit(self) -> None:
        tracked = {10: [COMMON, BOND_A]}
        once = build_reachability(tracked, [(10, COMMON)], [(10, BOND_A)])
        twice = build_reachability(
            tracked, [(10, COMMON), (10, COMMON)], [(10, BOND_A), (10, BOND_A)]
        )

        assert once.credited(10) == twice.credited(10)

    def test_counts_report_every_issuer(self) -> None:
        model = build_reachability(
            {1: [COMMON], 2: [BOND_A, BOND_B], 3: []},
            common_observations=[(1, COMMON)],
        )

        assert sum(model.counts().values()) == 3
        assert model.counts()["SAFE_SINGLE"] == 1
        assert model.counts()["MULTI_CLASS_UNASSIGNED"] == 1
        assert model.counts()["NO_CIK"] == 1
