from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from deep_research import (
    EXTERNAL_PREFIX,
    MAX_EXCERPT_CHARS,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    external_evidence_id,
    is_external,
)
from research import evidence_kind


@pytest.mark.unit
def test_evidence_id_carries_the_external_prefix_and_source_type() -> None:
    identifier = external_evidence_id(ExternalSourceType.NEWS, "https://example.test/a")

    assert identifier.startswith(f"{EXTERNAL_PREFIX}news.")


@pytest.mark.unit
def test_evidence_id_is_stable_across_calls_for_one_url() -> None:
    url = "https://example.test/acme-results"

    first = external_evidence_id(ExternalSourceType.NEWS, url)
    second = external_evidence_id(ExternalSourceType.NEWS, url)

    assert first == second


@pytest.mark.unit
def test_evidence_id_differs_between_urls() -> None:
    one = external_evidence_id(ExternalSourceType.NEWS, "https://example.test/a")
    other = external_evidence_id(ExternalSourceType.NEWS, "https://example.test/b")

    assert one != other


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("W.news.7f3a1c9e2b04", True),
        ("W.company.abc", True),
        ("W.", False),
        ("W", False),
        ("M.fcf_margin", False),
        ("X.0000320193-26-000073.mda", False),
        ("", False),
    ],
)
def test_is_external_recognises_only_a_populated_w_namespace(
    identifier: str, *, expected: bool
) -> None:
    assert is_external(identifier) is expected


@pytest.mark.unit
def test_phase_three_cannot_resolve_an_external_id() -> None:
    """The guarantee that keeps web evidence out of the deterministic layer.

    `research.EvidenceKind` has no `W.` member on purpose, so a web citation is
    unresolvable to the Phase 3 validator rather than being mistaken for a
    metric.
    """
    assert evidence_kind(external_evidence_id(ExternalSourceType.NEWS, "https://a.test")) is None


@pytest.mark.unit
def test_rejects_an_id_outside_the_external_namespace() -> None:
    with pytest.raises(ValidationError, match=r"must start with 'W\.'"):
        ExternalEvidence(
            evidence_id="M.revenue_growth_yoy",
            source_type=ExternalSourceType.NEWS,
            tier=SourceTier.TIER_2_REPUTABLE,
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="Something.",
            retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        )


@pytest.mark.unit
def test_rejects_a_naive_retrieval_timestamp() -> None:
    with pytest.raises(ValidationError, match="retrieved_at must be timezone-aware"):
        ExternalEvidence(
            evidence_id="W.news.abc123",
            source_type=ExternalSourceType.NEWS,
            tier=SourceTier.TIER_2_REPUTABLE,
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="Something.",
            retrieved_at=datetime(2026, 8, 14, 17, 0),  # noqa: DTZ001 — the case under test
        )


@pytest.mark.unit
def test_rejects_an_unknown_source_type() -> None:
    with pytest.raises(ValidationError, match="source_type"):
        ExternalEvidence(
            evidence_id="W.social.abc123",
            source_type="REDDIT",  # type: ignore[arg-type]  # the case under test
            tier=SourceTier.TIER_2_REPUTABLE,
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="Something.",
            retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        )


@pytest.mark.unit
def test_rejects_a_tier_outside_the_three() -> None:
    """V1 has no tier for social media, forums or blogs, and cannot be given one."""
    with pytest.raises(ValidationError, match="tier"):
        ExternalEvidence(
            evidence_id="W.news.abc123",
            source_type=ExternalSourceType.NEWS,
            tier="TIER_4_SOCIAL",  # type: ignore[arg-type]  # the case under test
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="Something.",
            retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        )


@pytest.mark.unit
def test_rejects_an_excerpt_longer_than_the_bound() -> None:
    with pytest.raises(ValidationError, match="excerpt"):
        ExternalEvidence(
            evidence_id="W.news.abc123",
            source_type=ExternalSourceType.NEWS,
            tier=SourceTier.TIER_2_REPUTABLE,
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="x" * (MAX_EXCERPT_CHARS + 1),
            retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        )


@pytest.mark.unit
def test_rejects_an_empty_excerpt() -> None:
    with pytest.raises(ValidationError, match="excerpt"):
        ExternalEvidence(
            evidence_id="W.news.abc123",
            source_type=ExternalSourceType.NEWS,
            tier=SourceTier.TIER_2_REPUTABLE,
            title="A headline",
            publisher="Example",
            url="https://example.test/a",
            excerpt="",
            retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        )


@pytest.mark.unit
def test_dated_prefers_the_event_over_its_reporting(
    make_external: Callable[..., ExternalEvidence],
) -> None:
    item = make_external(published_at=date(2026, 8, 12), occurred_at=date(2026, 8, 9))

    assert item.dated == date(2026, 8, 9)


@pytest.mark.unit
def test_dated_falls_back_to_publication(
    make_external: Callable[..., ExternalEvidence],
) -> None:
    item = make_external(published_at=date(2026, 8, 12), occurred_at=None)

    assert item.dated == date(2026, 8, 12)


@pytest.mark.unit
def test_age_is_none_for_an_undated_source(
    make_external: Callable[..., ExternalEvidence],
) -> None:
    item = make_external(published_at=None)

    assert item.age_in_days(date(2026, 8, 14)) is None


@pytest.mark.unit
def test_age_counts_days_back_to_publication(
    make_external: Callable[..., ExternalEvidence],
) -> None:
    item = make_external(published_at=date(2026, 8, 4))

    assert item.age_in_days(date(2026, 8, 14)) == 10
