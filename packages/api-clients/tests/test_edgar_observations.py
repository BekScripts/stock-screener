"""Provenance extraction: the filing dates the normalised path throws away.

`get_financial_statements` resolves every period to its most recently filed
value, which is what the metric engine needs. These cover the parallel read that
keeps the discarded facts, because a restatement is only visible in the
unresolved form.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from api_clients.edgar import (
    _COMMON_SHARES_DEI_TAGS,
    _IFRS_CONCEPTS,
    _US_GAAP_CONCEPTS,
    _detect_concept_set,
    _observations_for_tag,
    _statement_concepts,
    periods_from_facts_document,
    periods_from_observations,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from domain import FilingObservation


def _extract(document: Mapping[str, Any]) -> list[FilingObservation]:
    """Pull observations exactly as `get_filing_observations` does."""
    concepts = _detect_concept_set(document)
    assert concepts is not None
    found: list[FilingObservation] = []
    for namespace, tags in (
        (concepts.namespace, _statement_concepts(concepts)),
        (concepts.namespace, concepts.common_shares),
        ("dei", _COMMON_SHARES_DEI_TAGS),
    ):
        book = document.get(namespace, {})
        for tag in tags:
            found.extend(_observations_for_tag(book, namespace, tag))
    return found


@pytest.mark.unit
def test_a_restated_period_yields_two_observations() -> None:
    """The same quarter, two filings, two figures — both survive extraction."""
    book = {
        "Revenues": {
            "units": {
                "USD": [
                    {
                        "start": "2025-10-01",
                        "end": "2025-12-31",
                        "val": 100_000_000.0,
                        "accn": "0000320193-26-000001",
                        "fy": 2025,
                        "fp": "Q4",
                        "form": "10-K",
                        "filed": "2026-02-15",
                        "frame": "CY2025Q4",
                    },
                    {
                        "start": "2025-10-01",
                        "end": "2025-12-31",
                        "val": 104_000_000.0,
                        "accn": "0000320193-26-000009",
                        "fy": 2026,
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2026-04-30",
                    },
                ]
            }
        }
    }

    observations = _observations_for_tag(book, "us-gaap", "Revenues")

    assert [item.value for item in observations] == [100_000_000.0, 104_000_000.0]
    assert [item.filed_date for item in observations] == [
        date(2026, 2, 15),
        date(2026, 4, 30),
    ]


@pytest.mark.unit
def test_every_provenance_field_the_sec_publishes_is_kept() -> None:
    book = {
        "Revenues": {
            "units": {
                "USD": [
                    {
                        "start": "2025-10-01",
                        "end": "2025-12-31",
                        "val": 100.0,
                        "accn": "0000320193-26-000001",
                        "fy": 2025,
                        "fp": "Q4",
                        "form": "10-K",
                        "filed": "2026-02-15",
                        "frame": "CY2025Q4",
                    }
                ]
            }
        }
    }

    observation = _observations_for_tag(book, "us-gaap", "Revenues")[0]

    assert observation.taxonomy == "us-gaap"
    assert observation.concept == "Revenues"
    assert observation.unit == "USD"
    assert observation.period_start == date(2025, 10, 1)
    assert observation.period_end == date(2025, 12, 31)
    assert observation.accession == "0000320193-26-000001"
    assert observation.form == "10-K"
    assert observation.fiscal_year == 2025
    assert observation.fiscal_period == "Q4"
    assert observation.frame == "CY2025Q4"
    assert observation.source == "sec-edgar"


@pytest.mark.unit
def test_a_fact_without_a_filing_date_is_kept_with_none_not_dropped() -> None:
    """Its availability is unknown; that is recorded, not guessed at."""
    book = {"Revenues": {"units": {"USD": [{"end": "2025-09-30", "val": 90.0}]}}}

    observation = _observations_for_tag(book, "us-gaap", "Revenues")[0]

    assert observation.filed_date is None
    assert observation.value == 90.0


@pytest.mark.unit
def test_every_unit_is_read_not_only_the_money_one() -> None:
    """A share count is tagged in `shares`; a dual-currency filer tags both."""
    book = {
        "Revenues": {
            "units": {
                "USD": [{"end": "2025-12-31", "val": 100.0, "filed": "2026-02-15"}],
                "TWD": [{"end": "2025-12-31", "val": 3_200.0, "filed": "2026-02-15"}],
            }
        }
    }

    observations = _observations_for_tag(book, "us-gaap", "Revenues")

    assert {item.unit for item in observations} == {"USD", "TWD"}


@pytest.mark.unit
def test_a_fact_missing_a_period_end_or_a_value_is_skipped() -> None:
    book = {
        "Revenues": {
            "units": {
                "USD": [
                    {"val": 100.0, "filed": "2026-02-15"},
                    {"end": "2025-12-31", "filed": "2026-02-15"},
                    {"end": "2025-12-31", "val": 100.0, "filed": "2026-02-15"},
                ]
            }
        }
    }

    assert len(_observations_for_tag(book, "us-gaap", "Revenues")) == 1


@pytest.mark.unit
@pytest.mark.parametrize("book", [{}, {"Revenues": {}}, {"Revenues": {"units": []}}])
def test_a_missing_or_malformed_concept_yields_nothing(book: dict) -> None:
    assert _observations_for_tag(book, "us-gaap", "Revenues") == []


@pytest.mark.unit
def test_the_concept_scope_is_bounded_to_what_the_project_reads() -> None:
    """Not an XBRL warehouse: only the chains the statements are built from."""
    for concepts in (_US_GAAP_CONCEPTS, _IFRS_CONCEPTS):
        tags = _statement_concepts(concepts)
        assert 10 < len(tags) < 100
        assert len(tags) == len(set(tags))
        assert concepts.namespace not in tags
        # The cover-page share count lives in `dei` and is read separately.
        assert not set(tags) & set(concepts.common_shares)


@pytest.mark.unit
def test_rebuilding_a_document_reproduces_the_same_periods() -> None:
    """The equivalence the point-in-time engine rests on.

    If a rebuilt document normalised differently from the original, every
    historical metric would be a subtly different calculation wearing the same
    name. So the two paths are asserted equal rather than assumed equal.
    """
    document = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "val": 100_000_000.0,
                            "filed": "2025-05-01",
                            "form": "10-Q",
                        },
                        {
                            "start": "2025-04-01",
                            "end": "2025-06-30",
                            "val": 110_000_000.0,
                            "filed": "2025-08-01",
                            "form": "10-Q",
                        },
                    ]
                }
            },
            "GrossProfit": {
                "units": {
                    "USD": [
                        {
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "val": 40_000_000.0,
                            "filed": "2025-05-01",
                            "form": "10-Q",
                        }
                    ]
                }
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": "2025-03-31",
                            "val": 50_000_000.0,
                            "filed": "2025-05-01",
                            "form": "10-Q",
                        }
                    ]
                }
            }
        },
    }

    direct = periods_from_facts_document(document)
    rebuilt = periods_from_observations(_extract(document))

    assert direct == rebuilt
    assert direct


@pytest.mark.unit
def test_the_cover_page_share_count_survives_the_round_trip() -> None:
    """Both of its sources are captured.

    The normalisation reads this count from the filer's own taxonomy *and* from
    `dei`, under different tag names. Capturing only one leaves a rebuilt
    document without the figure market capitalisation is calculated from — which
    is what the extraction did before this test existed.
    """
    document = {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2025-10-01",
                            "end": "2025-12-31",
                            "val": 100_000_000.0,
                            "filed": "2026-02-15",
                            "form": "10-K",
                        }
                    ]
                }
            }
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": "2025-12-31",
                            "val": 50_000_000.0,
                            "filed": "2026-02-15",
                            "form": "10-K",
                        }
                    ]
                }
            }
        },
    }

    rebuilt = periods_from_observations(_extract(document))

    assert rebuilt[0].common_shares_outstanding == 50_000_000.0
    assert rebuilt == periods_from_facts_document(document)


@pytest.mark.unit
def test_an_empty_document_produces_no_periods() -> None:
    assert periods_from_observations([]) == []
    assert periods_from_facts_document({}) == []
