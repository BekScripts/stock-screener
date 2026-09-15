"""Planning which filings to fetch, and proving the plan is honest.

A planner is unusually easy to make look good. It can count a filing's coverage
backwards, count the same filing twice under two co-registrant CIKs, claim
months for an identity it cannot safely attribute, or produce an ordering that
depends on dictionary iteration and so cannot be reproduced by anyone checking
the work. Each of those is a test below.

The single rule the whole module rests on: **a filing may support a month-end at
or after its own filing date, within the carry-forward horizon, and never
before.** Everything else is bookkeeping.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from stock_screener.sec_evidence import (
    CARRY_FORWARD_DAYS,
    Candidate,
    GapClass,
    marginal_curve,
    month_end,
    plan_balanced,
    plan_coverage_first,
    plan_minimum_cover,
    plan_storage_efficient,
    supported_months,
    write_plan,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

IDENTITY = ("COMPANY", 1)
OTHER = ("COMPANY", 2)


def candidate(
    cik: int = 1,
    accession: str = "0000000001-24-000001",
    form: str = "8-K",
    filed: date = date(2024, 1, 15),
    *,
    covers: set[tuple[tuple[str, int], date]] | None = None,
    size: int = 40_000,
    yield_rate: float = 0.96,
) -> Candidate:
    """A planning candidate with an explicit coverage set."""
    if covers is None:
        covers = {(IDENTITY, m) for m in supported_months(filed)}
    return Candidate(
        cik=cik,
        accession=accession,
        form=form,
        filing_date=filed,
        estimated_bytes=size,
        yield_rate=yield_rate,
        covers=frozenset(covers),
    )


class TestCoverageIsForwardOnly:
    """The rule that makes every number in a plan meaningful."""

    def test_a_filing_supports_its_own_month_end(self) -> None:
        assert date(2024, 1, 31) in supported_months(date(2024, 1, 15))

    def test_a_filing_never_supports_an_earlier_month(self) -> None:
        """The failure that would make a plan look best while being worthless."""
        months = supported_months(date(2024, 6, 15))

        assert all(m >= date(2024, 6, 15) for m in months)
        assert date(2024, 5, 31) not in months
        assert date(2023, 12, 31) not in months

    def test_the_horizon_is_ninety_days_inclusive(self) -> None:
        months = supported_months(date(2024, 1, 1), horizon_days=90)

        assert date(2024, 3, 31) in months, "day 90 is inside the horizon"
        assert date(2024, 4, 30) not in months, "day 120 is not"

    def test_a_month_end_beyond_the_horizon_is_unsupported(self) -> None:
        """A 91-day gap is a gap, however nearly it closed."""
        months = supported_months(date(2024, 1, 2), horizon_days=90)

        assert date(2024, 4, 30) not in months

    def test_one_filing_can_cover_several_month_ends(self) -> None:
        months = supported_months(date(2024, 1, 5), horizon_days=90)

        assert len(months) >= 3
        assert months == tuple(sorted(months))

    def test_a_filing_on_a_month_end_covers_that_month(self) -> None:
        assert date(2024, 2, 29) in supported_months(date(2024, 2, 29))

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2024, 2, 10), date(2024, 2, 29)),
            (date(2023, 2, 10), date(2023, 2, 28)),
            (date(2024, 12, 1), date(2024, 12, 31)),
            (date(2024, 4, 30), date(2024, 4, 30)),
        ],
    )
    def test_month_end_handles_leap_years_and_december(self, day: date, expected: date) -> None:
        assert month_end(day) == expected


class TestSelectionIsDeterministic:
    def test_the_same_pool_produces_the_same_order(self) -> None:
        pool = [
            candidate(cik=c, accession=f"a-{c}", covers={(IDENTITY, date(2024, 1, 31))})
            for c in range(1, 30)
        ]

        first = plan_coverage_first(pool)
        second = plan_coverage_first(list(reversed(pool)))

        assert [(t.cik, t.accession) for t in first.targets] == [
            (t.cik, t.accession) for t in second.targets
        ]

    def test_the_plan_digest_is_stable(self, tmp_path: Path) -> None:
        pool = [
            candidate(cik=c, accession=f"a-{c}", covers={(IDENTITY, date(2024, c, 28))})
            for c in range(1, 9)
        ]
        plan = plan_coverage_first(pool)

        _, _, first = write_plan(tmp_path / "one", plan)
        _, _, second = write_plan(tmp_path / "two", plan)

        assert first == second
        assert len(first) == 64

    def test_ties_are_broken_all_the_way_to_the_accession(self) -> None:
        """Two candidates identical in every ranked field must still order."""
        a = candidate(cik=5, accession="zzz", covers={(IDENTITY, date(2024, 1, 31))})
        b = candidate(cik=5, accession="aaa", covers={(OTHER, date(2024, 1, 31))})

        plan = plan_coverage_first([a, b])

        assert len(plan.targets) == 2
        assert plan_coverage_first([b, a]).targets[0].accession == plan.targets[0].accession


class TestZeroBenefitCandidatesAreExcluded:
    def test_a_candidate_covering_nothing_is_never_selected(self) -> None:
        useful = candidate(accession="useful", covers={(IDENTITY, date(2024, 1, 31))})
        useless = candidate(accession="useless", covers=set())

        plan = plan_coverage_first([useful, useless])

        assert [t.accession for t in plan.targets] == ["useful"]

    def test_a_candidate_whose_months_are_already_taken_is_skipped(self) -> None:
        """The redundancy guarantee: no second filing for a month already pinned."""
        first = candidate(accession="first", covers={(IDENTITY, date(2024, 1, 31))})
        redundant = candidate(accession="redundant", covers={(IDENTITY, date(2024, 1, 31))})

        plan = plan_minimum_cover([first, redundant])

        assert len(plan.targets) == 1
        assert plan.potential_months == 1

    def test_minimum_cover_prefers_one_broad_filing_over_several_narrow(self) -> None:
        months = {date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)}
        broad = candidate(accession="broad", covers={(IDENTITY, m) for m in months})
        narrow = [
            candidate(accession=f"narrow-{i}", covers={(IDENTITY, m)})
            for i, m in enumerate(sorted(months))
        ]

        plan = plan_minimum_cover([*narrow, broad])

        assert plan.targets[0].accession == "broad"
        assert len(plan.targets) == 1, "the narrow filings add nothing once broad is taken"


class TestAccessionDeduplication:
    def test_one_filing_serving_two_identities_is_a_single_target(self) -> None:
        """A co-registrant or multi-class filing is downloaded once, not per security."""
        shared = candidate(covers={(IDENTITY, date(2024, 1, 31)), (OTHER, date(2024, 1, 31))})

        plan = plan_coverage_first([shared])

        assert len(plan.targets) == 1
        assert plan.targets[0].potential_months == 2

    def test_no_target_appears_twice(self) -> None:
        pool = [
            candidate(cik=c, accession=f"a-{c}", covers={(IDENTITY, date(2024, c, 28))})
            for c in range(1, 7)
        ]

        plan = plan_coverage_first([*pool, *pool])
        keys = [(t.cik, t.accession) for t in plan.targets]

        assert len(keys) == len(set(keys))


class TestYieldAdjustment:
    def test_potential_and_expected_are_reported_separately(self) -> None:
        """A plan must never present an unread filing's coverage as certain."""
        low = candidate(form="20-F", yield_rate=0.695, covers={(IDENTITY, date(2024, 1, 31))})

        plan = plan_coverage_first([low])

        assert plan.potential_months == 1
        assert plan.yield_adjusted_months == pytest.approx(0.695)
        assert plan.yield_adjusted_months < plan.potential_months

    def test_a_higher_yield_form_wins_an_otherwise_equal_tie(self) -> None:
        # Same marginal coverage, different identities so both survive.
        weak = candidate(
            cik=1,
            accession="weak",
            form="20-F",
            yield_rate=0.60,
            covers={(IDENTITY, date(2024, 1, 31))},
        )
        strong = candidate(
            cik=2,
            accession="strong",
            form="8-K",
            yield_rate=0.96,
            covers={(OTHER, date(2024, 1, 31))},
        )

        plan = plan_coverage_first([weak, strong])

        assert plan.targets[0].accession == "strong"


class TestStorageEfficientPlan:
    def test_a_marginal_floor_keeps_trivial_filings_out(self) -> None:
        """Pure months-per-byte would rank a tiny one-month filing top forever."""
        tiny = candidate(
            cik=1, accession="tiny", size=1_000, covers={(IDENTITY, date(2024, 1, 31))}
        )
        useful = candidate(
            cik=2,
            accession="useful",
            size=2_000_000,
            covers={(OTHER, date(2024, 1, 31)), (OTHER, date(2024, 2, 29))},
        )

        plan = plan_storage_efficient([tiny, useful], min_marginal=2)

        assert [t.accession for t in plan.targets] == ["useful"]

    def test_efficiency_prefers_the_cheaper_of_two_equal_covers(self) -> None:
        cheap = candidate(
            cik=1,
            accession="cheap",
            size=40_000,
            covers={(IDENTITY, date(2024, 1, 31)), (IDENTITY, date(2024, 2, 29))},
        )
        dear = candidate(
            cik=2,
            accession="dear",
            size=4_000_000,
            covers={(OTHER, date(2024, 1, 31)), (OTHER, date(2024, 2, 29))},
        )

        plan = plan_storage_efficient([dear, cheap])

        assert plan.targets[0].accession == "cheap"


class TestBalancedPlanAndCurve:
    def test_the_balanced_plan_covers_less_than_coverage_first(self) -> None:
        """Its floor is the trade: fewer filings, slightly less coverage."""
        pool = [
            candidate(
                cik=1, accession="broad", covers={(IDENTITY, date(2024, m, 28)) for m in (1, 2, 3)}
            ),
            candidate(cik=2, accession="single", covers={(OTHER, date(2024, 5, 31))}),
        ]

        first = plan_coverage_first(pool)
        balanced = plan_balanced(pool)

        assert first.potential_months == 4
        assert balanced.potential_months == 3, "the single-month filing is below the floor"
        assert len(balanced.targets) < len(first.targets)

    def test_the_curve_is_monotonic_and_reads_the_plans_own_order(self) -> None:
        pool = [
            candidate(cik=c, accession=f"a-{c}", covers={(("COMPANY", c), date(2024, 1, 31))})
            for c in range(1, 12)
        ]
        plan = plan_coverage_first(pool)

        curve = marginal_curve(plan, [1, 3, 5, len(plan.targets)])

        assert [row["filings"] for row in curve] == [1, 3, 5, len(plan.targets)]
        months = [row["potential_months"] for row in curve]
        assert months == sorted(months)


class TestPlanArtifacts:
    def test_a_written_plan_round_trips_to_csv_and_jsonl(self, tmp_path: Path) -> None:
        plan = plan_coverage_first([candidate()])

        csv_path, jsonl_path, sha = write_plan(tmp_path, plan)

        assert csv_path.is_file() and jsonl_path.is_file()
        assert (tmp_path / "expansion_plan_v1.sha256").read_text().startswith(sha)
        assert "already_downloaded" in csv_path.read_text()

    def test_every_target_is_marked_not_downloaded(self) -> None:
        plan = plan_coverage_first([candidate()])

        assert all(not t.already_downloaded for t in plan.targets)

    def test_a_target_records_the_months_it_was_chosen_for(self) -> None:
        plan = plan_coverage_first([candidate(filed=date(2024, 1, 15))])
        target = plan.targets[0]

        assert target.first_target_month == "2024-01-31"
        assert target.last_target_month is not None
        assert target.last_target_month >= target.first_target_month

    def test_gap_classes_keep_solvable_apart_from_unreachable(self) -> None:
        """Pooling these would let a plan claim months no download can close."""
        assert len({str(c) for c in GapClass}) == len(list(GapClass)), "no two classes collide"
        assert {str(c) for c in GapClass} == {
            "G1_SOLVABLE",
            "G2_NO_CIK",
            "G3_AMBIGUOUS",
            "G4_NO_CANDIDATE",
            "G5_OTHER",
        }

    def test_the_carry_forward_constant_is_the_retained_policy(self) -> None:
        """9L.1 measured and kept 90 days; a silent change would invalidate the plan."""
        assert CARRY_FORWARD_DAYS == 90
