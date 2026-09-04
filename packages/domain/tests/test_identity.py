"""Adversarial tests for the security-identity layer.

Each test names a way identity could quietly become wrong and asserts the code
refuses it: two companies spliced under one symbol, one company split in two by a
reverse split, a retired identifier still answering, an absence of evidence read
as evidence of absence.

The cases are the ones the Phase 9A survivorship audit actually found, not
invented shapes. `BBBY` is Bed Bath & Beyond and the unrelated issuer that took
its symbol; the CUSIPs are real ones, so the issuer bases and check digits are
the real arithmetic rather than a convenient fiction.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    FTD_THRESHOLD_LIFTED,
    EvidenceVerdict,
    FtdIdentityEvidence,
    IdentifierInterval,
    IdentityConfidence,
    LineageKind,
    ResolutionStatus,
    SymbolStatus,
    classify_symbol,
    cusip_issuer_base,
    existence_evidence,
    intervals_for_evidence,
    is_valid_cusip,
    overlapping_intervals,
    resolve_by_cusip,
    resolve_by_ticker,
    succeed_interval,
)

#: Bed Bath & Beyond Inc. Its equity was extinguished on 2023-05-02.
BBBY_CUSIP = "075896100"

#: Beyond, Inc. — the unrelated issuer trading under `BBBY` since 2025-09-02.
#: Note the different six-character issuer base, which is what makes the two
#: provably distinct rather than merely suspected so. Both CUSIPs are the real
#: ones the SEC's fails record carries for that symbol.
OTHER_BBBY_CUSIP = "690370101"


def evidence(
    *,
    cusip: str,
    symbol: str,
    name: str,
    first: date,
    last: date,
    observations: int = 10,
) -> FtdIdentityEvidence:
    """Build one identity triple, so the tests read as the case they describe."""
    return FtdIdentityEvidence(
        cusip=cusip,
        symbol=symbol,
        issuer_name=name,
        first_settlement=first,
        last_settlement=last,
        observations=observations,
        source_file="cnsfails-test",
        evidence_hash="0" * 64,
    )


def interval(
    *,
    security_id: int,
    cusip: str | None = None,
    ticker: str | None = None,
    valid_from: date,
    valid_to: date | None = None,
    lineage: LineageKind = LineageKind.INITIAL,
) -> IdentifierInterval:
    """Build one identifier interval with the fields a test actually varies."""
    return IdentifierInterval(
        security_id=security_id,
        cusip=cusip,
        ticker=ticker,
        venue=None,
        valid_from=valid_from,
        valid_to=valid_to,
        source="test",
        confidence=IdentityConfidence.OBSERVED,
        lineage=lineage,
    )


class TestCusipIsNotAnIdentity:
    """The premise of the layer: a CUSIP names an issue, not a lineage."""

    def test_the_issuer_base_is_the_first_six_characters(self) -> None:
        assert cusip_issuer_base(BBBY_CUSIP) == "075896"

    def test_two_issues_of_one_issuer_share_a_base(self) -> None:
        # A reverse split remints the issue digits and leaves the issuer alone.
        assert cusip_issuer_base("075896309") == cusip_issuer_base(BBBY_CUSIP)

    def test_two_issuers_do_not(self) -> None:
        assert cusip_issuer_base(OTHER_BBBY_CUSIP) != cusip_issuer_base(BBBY_CUSIP)

    def test_a_cusip_is_checked_against_its_own_check_digit(self) -> None:
        assert is_valid_cusip("037833100")  # Apple Inc.
        assert is_valid_cusip("Y2573F102")  # a CINS, whose letters carry values
        assert not is_valid_cusip("037833101")

    def test_a_malformed_cusip_is_rejected_not_repaired(self) -> None:
        # There is deliberately no function that returns a corrected identifier.
        assert not is_valid_cusip("12345")
        assert not is_valid_cusip("!!!!!!!!!")


class TestATickerChangePreservesTheSecurity:
    """Requirement 1. `FB` became `META` and did not become a second company."""

    def test_a_ticker_change_keeps_one_security_id(self) -> None:
        original = interval(
            security_id=7, cusip="30303M102", ticker="FB", valid_from=date(2012, 5, 18)
        )
        closed, successor = succeed_interval(
            original,
            effective_date=date(2022, 6, 9),
            ticker="META",
            lineage=LineageKind.TICKER_CHANGE,
            source="test",
        )

        assert closed.security_id == successor.security_id == 7
        assert successor.cusip == original.cusip
        assert successor.lineage is LineageKind.TICKER_CHANGE

    def test_both_symbols_resolve_to_that_one_security_on_their_own_dates(self) -> None:
        original = interval(
            security_id=7, cusip="30303M102", ticker="FB", valid_from=date(2012, 5, 18)
        )
        intervals = succeed_interval(
            original,
            effective_date=date(2022, 6, 9),
            ticker="META",
            lineage=LineageKind.TICKER_CHANGE,
            source="test",
        )

        before = resolve_by_ticker(intervals, "FB", date(2020, 1, 2))
        after = resolve_by_ticker(intervals, "META", date(2024, 1, 2))

        assert before.security_id == after.security_id == 7

    def test_the_old_symbol_stops_answering_after_the_change(self) -> None:
        original = interval(
            security_id=7, cusip="30303M102", ticker="FB", valid_from=date(2012, 5, 18)
        )
        intervals = succeed_interval(
            original,
            effective_date=date(2022, 6, 9),
            ticker="META",
            lineage=LineageKind.TICKER_CHANGE,
            source="test",
        )

        assert resolve_by_ticker(intervals, "FB", date(2024, 1, 2)).status is (
            ResolutionStatus.UNRESOLVED
        )


class TestATickerReuseIsADifferentSecurity:
    """Requirement 2. One symbol, two issuers, and no join between them."""

    def test_two_issuers_under_one_symbol_are_two_securities(self) -> None:
        verdict = classify_symbol(
            "BBBY",
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="BBBY",
                    name="BED BATH & BEYOND INC",
                    first=date(2019, 1, 3),
                    last=date(2023, 5, 2),
                ),
                evidence(
                    cusip=OTHER_BBBY_CUSIP,
                    symbol="BBBY",
                    name="AN UNRELATED ISSUER INC",
                    first=date(2025, 7, 14),
                    last=date(2026, 6, 30),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.REUSED
        assert len(verdict.spans) == 2
        assert {span.issuer_base for span in verdict.spans} == {"075896", "690370"}

    def test_the_symbol_names_one_of_them_only_with_a_date(self) -> None:
        intervals = (
            interval(
                security_id=1,
                cusip=BBBY_CUSIP,
                ticker="BBBY",
                valid_from=date(2019, 1, 3),
                valid_to=date(2023, 5, 2),
            ),
            interval(
                security_id=2,
                cusip=OTHER_BBBY_CUSIP,
                ticker="BBBY",
                valid_from=date(2025, 7, 14),
            ),
        )

        assert resolve_by_ticker(intervals, "BBBY", date(2022, 6, 30)).security_id == 1
        assert resolve_by_ticker(intervals, "BBBY", date(2025, 9, 30)).security_id == 2

    def test_the_gap_between_them_resolves_to_neither(self) -> None:
        intervals = (
            interval(
                security_id=1,
                cusip=BBBY_CUSIP,
                ticker="BBBY",
                valid_from=date(2019, 1, 3),
                valid_to=date(2023, 5, 2),
            ),
            interval(
                security_id=2,
                cusip=OTHER_BBBY_CUSIP,
                ticker="BBBY",
                valid_from=date(2025, 7, 14),
            ),
        )

        # The 26-month hole in `companies` row 2147. Neither company traded under
        # the symbol then, and inventing an occupant is what the audit found.
        assert resolve_by_ticker(intervals, "BBBY", date(2024, 6, 28)).status is (
            ResolutionStatus.UNRESOLVED
        )

    def test_a_reuse_cannot_be_spliced_into_one_security(self) -> None:
        """Requirement 5, stated as the operation that would do the splicing."""
        both = [
            evidence(
                cusip=BBBY_CUSIP,
                symbol="BBBY",
                name="BED BATH & BEYOND INC",
                first=date(2019, 1, 3),
                last=date(2023, 5, 2),
            ),
            evidence(
                cusip=OTHER_BBBY_CUSIP,
                symbol="BBBY",
                name="AN UNRELATED ISSUER INC",
                first=date(2025, 7, 14),
                last=date(2026, 6, 30),
            ),
        ]

        with pytest.raises(ValueError, match="spans 2 CUSIP issuers"):
            intervals_for_evidence(1, "BBBY", both, source="test")

    def test_overlapping_windows_are_ambiguous_rather_than_reused(self) -> None:
        verdict = classify_symbol(
            "DUPE",
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="DUPE",
                    name="ONE INC",
                    first=date(2020, 1, 2),
                    last=date(2022, 1, 2),
                ),
                evidence(
                    cusip=OTHER_BBBY_CUSIP,
                    symbol="DUPE",
                    name="TWO INC",
                    first=date(2021, 1, 2),
                    last=date(2023, 1, 2),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.AMBIGUOUS


class TestAReverseSplitPreservesTheSecurity:
    """Requirement 3, and requirement 4 — the retired CUSIP must stop answering."""

    def test_a_new_cusip_from_a_reverse_split_keeps_the_security_id(self) -> None:
        before = interval(
            security_id=11, cusip="075896100", ticker="RSPL", valid_from=date(2018, 1, 2)
        )
        closed, successor = succeed_interval(
            before,
            effective_date=date(2023, 3, 1),
            cusip="075896209",
            lineage=LineageKind.REVERSE_SPLIT,
            source="test",
        )

        assert successor.security_id == closed.security_id == 11
        assert successor.cusip != closed.cusip
        assert successor.ticker == closed.ticker == "RSPL"
        assert successor.lineage is LineageKind.REVERSE_SPLIT

    def test_both_cusips_resolve_to_the_same_security_on_their_own_dates(self) -> None:
        before = interval(
            security_id=11, cusip="075896100", ticker="RSPL", valid_from=date(2018, 1, 2)
        )
        intervals = succeed_interval(
            before,
            effective_date=date(2023, 3, 1),
            cusip="075896209",
            lineage=LineageKind.REVERSE_SPLIT,
            source="test",
        )

        old = resolve_by_cusip(intervals, "075896100", date(2020, 6, 1))
        new = resolve_by_cusip(intervals, "075896209", date(2024, 6, 1))

        assert old.security_id == new.security_id == 11

    def test_the_retired_cusip_cannot_leak_past_its_interval(self) -> None:
        """Requirement 4. The old identifier stops naming the security."""
        before = interval(
            security_id=11, cusip="075896100", ticker="RSPL", valid_from=date(2018, 1, 2)
        )
        intervals = succeed_interval(
            before,
            effective_date=date(2023, 3, 1),
            cusip="075896209",
            lineage=LineageKind.REVERSE_SPLIT,
            source="test",
        )

        after = resolve_by_cusip(intervals, "075896100", date(2024, 6, 1))

        assert after.status is ResolutionStatus.UNRESOLVED
        assert after.security_id is None

    def test_the_closed_interval_ends_the_day_before_its_successor(self) -> None:
        before = interval(
            security_id=11, cusip="075896100", ticker="RSPL", valid_from=date(2018, 1, 2)
        )
        closed, successor = succeed_interval(
            before,
            effective_date=date(2023, 3, 1),
            cusip="075896209",
            lineage=LineageKind.REVERSE_SPLIT,
            source="test",
        )

        assert closed.valid_to == date(2023, 2, 28)
        assert successor.valid_from == date(2023, 3, 1)
        # No date is covered twice, and none falls between them.
        assert not closed.covers(date(2023, 3, 1))
        assert not successor.covers(date(2023, 2, 28))

    def test_a_successor_dated_before_its_predecessor_began_is_refused(self) -> None:
        before = interval(
            security_id=11, cusip="075896100", ticker="RSPL", valid_from=date(2018, 1, 2)
        )

        with pytest.raises(ValueError, match="not after the interval it supersedes"):
            succeed_interval(
                before,
                effective_date=date(2017, 1, 1),
                lineage=LineageKind.REVERSE_SPLIT,
                source="test",
            )


class TestEvidenceAndItsAbsence:
    """Requirements 6, 7 and 8: what the record can and cannot be made to say."""

    def test_a_positive_match_proves_the_security_traded(self) -> None:
        verdict = existence_evidence(
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="BBBY",
                    name="BED BATH & BEYOND INC",
                    first=date(2019, 1, 3),
                    last=date(2023, 5, 2),
                    observations=412,
                )
            ]
        )

        assert verdict.verdict is EvidenceVerdict.PRESENT
        assert verdict.proves_existence
        assert verdict.observations == 412

    def test_no_match_never_says_the_security_did_not_exist(self) -> None:
        """Requirement 7. Silence about deliveries is not silence about listing."""
        verdict = existence_evidence([])

        assert verdict.verdict is EvidenceVerdict.UNPROVEN
        assert not verdict.proves_existence
        assert "establishes nothing" in verdict.detail

    def test_there_is_no_verdict_that_could_record_non_existence(self) -> None:
        assert set(EvidenceVerdict) == {EvidenceVerdict.PRESENT, EvidenceVerdict.UNPROVEN}

    def test_an_absence_before_the_threshold_was_lifted_is_flagged_as_censored(self) -> None:
        verdict = existence_evidence([], window_start=date(2005, 1, 3))

        assert verdict.verdict is EvidenceVerdict.UNPROVEN
        assert verdict.threshold_censored
        assert str(FTD_THRESHOLD_LIFTED) in verdict.detail

    def test_an_absence_after_it_was_lifted_is_not(self) -> None:
        verdict = existence_evidence([], window_start=date(2015, 1, 2))

        assert not verdict.threshold_censored

    def test_a_security_identity_needs_no_cik_when_cusip_evidence_exists(self) -> None:
        """Requirement 6. The domain's identity is an id; a CIK is never asked for."""
        rows = [
            evidence(
                cusip=BBBY_CUSIP,
                symbol="BBBY",
                name="BED BATH & BEYOND INC",
                first=date(2019, 1, 3),
                last=date(2023, 5, 2),
            )
        ]

        intervals = intervals_for_evidence(1, "BBBY", rows, source="SEC_FTD")

        assert len(intervals) == 1
        assert intervals[0].security_id == 1
        assert intervals[0].cusip == BBBY_CUSIP
        assert resolve_by_ticker(intervals, "BBBY", date(2021, 6, 1)).security_id == 1

    def test_a_symbol_with_no_evidence_is_unresolved_not_absent(self) -> None:
        """Requirement 8's other half: no evidence produces a status, not a guess."""
        verdict = classify_symbol("NOEV", [])

        assert verdict.status is SymbolStatus.UNRESOLVED
        assert verdict.spans == ()

    def test_disagreeing_evidence_produces_ambiguity_rather_than_a_choice(self) -> None:
        """Requirement 8. Two issuers at once is a status, never a preference."""
        verdict = classify_symbol(
            "CLASH",
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="CLASH",
                    name="ONE INC",
                    first=date(2020, 1, 2),
                    last=date(2021, 6, 30),
                ),
                evidence(
                    cusip=OTHER_BBBY_CUSIP,
                    symbol="CLASH",
                    name="TWO CORP",
                    first=date(2020, 6, 1),
                    last=date(2022, 1, 4),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.AMBIGUOUS
        assert "overlapping windows" in verdict.detail

    def test_two_securities_claiming_one_identifier_at_once_is_reported(self) -> None:
        conflicts = overlapping_intervals(
            [
                interval(
                    security_id=1,
                    cusip=BBBY_CUSIP,
                    ticker="BBBY",
                    valid_from=date(2019, 1, 3),
                    valid_to=date(2023, 5, 2),
                ),
                interval(
                    security_id=2,
                    cusip=BBBY_CUSIP,
                    ticker="BBBY",
                    valid_from=date(2022, 1, 3),
                    valid_to=date(2024, 5, 2),
                ),
            ]
        )

        assert len(conflicts) == 1

    def test_two_intervals_of_one_security_are_not_a_conflict(self) -> None:
        assert (
            overlapping_intervals(
                [
                    interval(
                        security_id=1,
                        cusip="075896100",
                        ticker="RSPL",
                        valid_from=date(2018, 1, 2),
                        valid_to=date(2023, 2, 28),
                    ),
                    interval(
                        security_id=1,
                        cusip="075896209",
                        ticker="RSPL",
                        valid_from=date(2023, 3, 1),
                    ),
                ]
            )
            == ()
        )


class TestIntervalsAreNeverInterpolated:
    """Every endpoint is a date the record actually carries."""

    def test_each_cusip_gets_the_window_its_own_evidence_shows(self) -> None:
        rows = [
            evidence(
                cusip="075896100",
                symbol="RSPL",
                name="ONE INC",
                first=date(2018, 1, 2),
                last=date(2023, 2, 27),
            ),
            evidence(
                cusip="075896209",
                symbol="RSPL",
                name="ONE INC",
                first=date(2023, 3, 6),
                last=date(2024, 9, 30),
            ),
        ]

        intervals = intervals_for_evidence(3, "RSPL", rows, source="SEC_FTD")

        assert [(i.valid_from, i.valid_to) for i in intervals] == [
            (date(2018, 1, 2), date(2023, 2, 27)),
            (date(2023, 3, 6), date(2024, 9, 30)),
        ]

    def test_a_second_cusip_of_one_issuer_is_a_cusip_change_not_a_split(self) -> None:
        # The fails file records no corporate action, so naming the cause would
        # be a guess. What it does establish is that the security is the same.
        rows = [
            evidence(
                cusip="075896100",
                symbol="RSPL",
                name="ONE INC",
                first=date(2018, 1, 2),
                last=date(2023, 2, 27),
            ),
            evidence(
                cusip="075896209",
                symbol="RSPL",
                name="ONE INC",
                first=date(2023, 3, 6),
                last=date(2024, 9, 30),
            ),
        ]

        intervals = intervals_for_evidence(3, "RSPL", rows, source="SEC_FTD")

        assert intervals[1].lineage is LineageKind.CUSIP_CHANGE
        assert intervals[1].confidence is IdentityConfidence.INFERRED
        assert {i.security_id for i in intervals} == {3}

    def test_evidence_for_no_cusip_at_all_cannot_build_an_interval(self) -> None:
        with pytest.raises(ValueError, match="no evidence"):
            intervals_for_evidence(3, "NONE", [], source="SEC_FTD")


#: Seattle Genetics and Seagen, as the SEC's fails record actually carries them.
#: The issuer bases differ, so these are two spans; their evidence windows meet on
#: 2020-10-09, which is the handover and not an overlap.
SEATTLE_GENETICS_CUSIP = "812578102"
SEAGEN_CUSIP = "81181C104"


class TestAHandoverIsNotAConcurrentClaim:
    """Phase 9J. Contact at a single settlement date orders two occupants; it
    does not make them rivals."""

    def test_windows_touching_at_one_day_are_reuse_not_ambiguity(self) -> None:
        verdict = classify_symbol(
            "SGEN",
            [
                evidence(
                    cusip=SEATTLE_GENETICS_CUSIP,
                    symbol="SGEN",
                    name="SEATTLE GENETICS INC",
                    first=date(2004, 5, 24),
                    last=date(2020, 10, 9),
                ),
                evidence(
                    cusip=SEAGEN_CUSIP,
                    symbol="SGEN",
                    name="SEAGEN INC COM (DE)",
                    first=date(2020, 10, 9),
                    last=date(2023, 12, 15),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.REUSED

    def test_windows_overlapping_by_two_days_stay_ambiguous(self) -> None:
        verdict = classify_symbol(
            "TWODAY",
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="TWODAY",
                    name="ONE INC",
                    first=date(2020, 1, 2),
                    last=date(2020, 10, 9),
                ),
                evidence(
                    cusip=OTHER_BBBY_CUSIP,
                    symbol="TWODAY",
                    name="TWO INC",
                    first=date(2020, 10, 8),
                    last=date(2023, 12, 15),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.AMBIGUOUS

    def test_a_handover_still_leaves_the_two_issuers_as_separate_spans(self) -> None:
        verdict = classify_symbol(
            "SGEN",
            [
                evidence(
                    cusip=SEATTLE_GENETICS_CUSIP,
                    symbol="SGEN",
                    name="SEATTLE GENETICS INC",
                    first=date(2004, 5, 24),
                    last=date(2020, 10, 9),
                ),
                evidence(
                    cusip=SEAGEN_CUSIP,
                    symbol="SGEN",
                    name="SEAGEN INC COM (DE)",
                    first=date(2020, 10, 9),
                    last=date(2023, 12, 15),
                ),
            ],
        )

        assert len(verdict.spans) == 2
        assert {span.issuer_base for span in verdict.spans} == {"812578", "81181C"}

    def test_a_two_year_gap_between_occupants_is_still_reuse(self) -> None:
        verdict = classify_symbol(
            "BBBY",
            [
                evidence(
                    cusip=BBBY_CUSIP,
                    symbol="BBBY",
                    name="BED BATH & BEYOND",
                    first=date(2004, 3, 24),
                    last=date(2023, 5, 3),
                ),
                evidence(
                    cusip=OTHER_BBBY_CUSIP,
                    symbol="BBBY",
                    name="BED BATH & BEYOND, INC",
                    first=date(2025, 9, 2),
                    last=date(2026, 8, 12),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.REUSED
        assert len(verdict.spans) == 2

    def test_an_eight_hundred_day_overlap_is_never_a_handover(self) -> None:
        """`BNKD`, whose two issuers really did claim the symbol at once."""
        verdict = classify_symbol(
            "BNKD",
            [
                evidence(
                    cusip="063679108",
                    symbol="BNKD",
                    name="BMO ETN",
                    first=date(2019, 4, 8),
                    last=date(2026, 8, 13),
                ),
                evidence(
                    cusip="06367V102",
                    symbol="BNKD",
                    name="MICROSECTORS",
                    first=date(2024, 4, 30),
                    last=date(2024, 7, 25),
                ),
            ],
        )

        assert verdict.status is SymbolStatus.AMBIGUOUS


class TestOneIssuerBaseIsNeverSplitAcrossSecurities:
    """The other half of the repair: nothing here merges what the evidence keeps
    apart, and the share-class pairs must survive it unchanged."""

    @pytest.mark.parametrize(
        ("symbol", "left", "right"),
        [
            ("GOOGL", "38259P508", "02079K305"),
            ("BKEP", "09625U109", "09625U208"),
        ],
    )
    def test_distinct_full_cusips_remain_distinct_evidence(
        self, symbol: str, left: str, right: str
    ) -> None:
        verdict = classify_symbol(
            symbol,
            [
                evidence(
                    cusip=left,
                    symbol=symbol,
                    name="FIRST",
                    first=date(2004, 3, 22),
                    last=date(2015, 4, 2),
                ),
                evidence(
                    cusip=right,
                    symbol=symbol,
                    name="SECOND",
                    first=date(2015, 4, 2),
                    last=date(2026, 8, 12),
                ),
            ],
        )

        assert {c for span in verdict.spans for c in span.cusips} == {left, right}
