"""Shared pytest fixtures for the root application test suite.

`Make` builds synthetic price and fundamental history; `Seed` writes it into a
database along with a score snapshot, so a test that needs a scored company
starts from one rather than from twenty lines of setup.

Both live here rather than in a second `conftest.py` under `tests/integration/`:
mypy refuses two modules with the same name on one path, and one shared file is
cheaper than the package plumbing that would silence it.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from api_clients import MockFundamentals, MockMarketData
from api_clients.errors import ProviderRequestError
from api_clients.sec_documents import FetchedDocument
from data_access import (
    PRELIMINARY,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyMetrics,
    CompanyProfile,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    FinancialPeriod,
    MarketCapSource,
    MetricUnit,
    PriceBar,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    SubScore,
    ValuationBasis,
    VolumeBasis,
)
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    TICKER_MAP_URL,
    ArtifactNotFoundError,
    ArtifactStat,
    EvidenceWriteError,
    IdentityRow,
    StorageUnavailableError,
    StoredArtifact,
    validate_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence import EvidenceStorage


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--live-b2` opts in to the tests that make real Backblaze requests."""
    parser.addoption(
        "--live-b2",
        action="store_true",
        default=False,
        help="run tests marked live_b2 against the configured Backblaze bucket",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `live_b2` tests unless explicitly opted in. `make check` never opts in."""
    if config.getoption("--live-b2"):
        return
    skip = pytest.mark.skip(reason="live Backblaze test; pass --live-b2 to run it")
    for item in items:
        if "live_b2" in item.keywords:
            item.add_marker(skip)


#: Every variable `Settings` reads. A field added to `Settings` without a line
#: here leaks the developer's real environment into the tests, and the failure
#: looks like a bug in the code rather than in the fixture.
_SETTINGS_ENV_VARS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "LOG_JSON",
    "DATABASE_URL",
    "MARKET_DATA_PROVIDER",
    "FUNDAMENTALS_PROVIDER",
    "FIXTURE_PATH",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_TRADING_BASE_URL",
    "ALPACA_DATA_BASE_URL",
    "ALPACA_FEED",
    "FUNDAMENTALS_API_KEY",
    "FUNDAMENTALS_BASE_URL",
    "FUNDAMENTALS_API_ROOT",
    "SEC_USER_AGENT",
    "SEC_EVIDENCE_DIR",
    "SEC_CONTROL_DIR",
    "SEC_STORAGE_BACKEND",
    "B2_ENDPOINT_URL",
    "B2_BUCKET",
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    "PROVIDER_MAX_ATTEMPTS",
    "PROVIDER_RETRY_BACKOFF_SECONDS",
    "PROVIDER_MIN_REQUEST_INTERVAL_SECONDS",
    "PROVIDER_BATCH_SIZE",
    "PROVIDER_TIMEOUT_SECONDS",
    "PRICE_HISTORY_DAYS",
    "FUNDAMENTALS_QUARTERS",
    "BENCHMARK_SYMBOL",
    "FMP_ENRICHMENT_LIMIT",
    "RESEARCH_PROVIDER",
    "RESEARCH_API_KEY",
    "RESEARCH_MODEL",
    "RESEARCH_MAX_OUTPUT_TOKENS",
    "RESEARCH_EFFORT",
    "RESEARCH_TIMEOUT_SECONDS",
    "RESEARCH_MAX_RUN_COST_USD",
    "EXTERNAL_RESEARCH_PROVIDER",
    "EXTERNAL_RESEARCH_API_KEY",
    "EXTERNAL_RESEARCH_BASE_URL",
    "EXTERNAL_RESEARCH_WINDOW_DAYS",
    "EXTERNAL_RESEARCH_MAX_ITEMS",
    "EXTERNAL_RESEARCH_MAX_PER_DOMAIN",
    "EXTERNAL_RESEARCH_MAX_RESULTS",
    "EXTERNAL_RESEARCH_TIMEOUT_SECONDS",
    "DEEP_RESEARCH_PROVIDER",
    "DEEP_RESEARCH_MODEL",
    "DEEP_RESEARCH_MAX_INPUT_TOKENS",
    "DEEP_RESEARCH_MAX_OUTPUT_TOKENS",
    "DEEP_RESEARCH_EFFORT",
    "DEEP_RESEARCH_TIMEOUT_SECONDS",
    "DEEP_RESEARCH_MAX_COST_USD",
    "DEEP_RESEARCH_EXTERNAL_CACHE_HOURS",
    "MIN_PRICE",
    "MIN_MARKET_CAP",
    "MIN_AVG_DOLLAR_VOLUME",
    "MIN_TRADING_DAYS",
)

LATEST_SESSION = date(2026, 6, 30)
"""The date every synthetic history ends on, so tests never depend on today."""


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every test from the developer's real environment.

    Moves to an empty working directory so no `.env` file is picked up, and
    clears the variables `Settings` reads. Tests that want a value set it
    explicitly with `monkeypatch.setenv`. The settings cache is cleared too, or a
    value read by an earlier test would survive into this one.
    """
    monkeypatch.chdir(tmp_path)
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Return settings pinned to the test environment."""
    return Settings(environment="test")


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    open_session = factory()
    try:
        yield open_session
    finally:
        open_session.close()
        engine.dispose()


class Make:
    """Builders for synthetic price and fundamental history.

    Reached through the `make` fixture rather than imported:
    `--import-mode=importlib` means a test module cannot `from conftest import`,
    and a fixture is the supported way to share helpers across a directory.
    """

    latest_session = LATEST_SESSION

    @staticmethod
    def bars(sessions: int, close: float = 25.0, volume: float = 500_000.0) -> list[PriceBar]:
        """Build consecutive daily bars ending at `LATEST_SESSION`."""
        return [
            PriceBar(
                date=LATEST_SESSION - timedelta(days=offset),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=volume,
            )
            for offset in reversed(range(sessions))
        ]

    @staticmethod
    def quarters(count: int = 8, revenue: float = 100_000_000.0) -> list[FinancialPeriod]:
        """Build consecutive quarterly periods ending at `LATEST_SESSION`."""
        return [
            FinancialPeriod(
                period_end=LATEST_SESSION - timedelta(days=91 * (count - 1 - index)),
                revenue=revenue,
                gross_profit=revenue * 0.4,
                operating_income=revenue * 0.1,
                operating_cash_flow=revenue * 0.12,
                capital_expenditure=revenue * 0.02,
                cash=revenue * 2,
                total_debt=revenue * 0.5,
                shares_outstanding=41_000_000.0,
                source="test",
            )
            for index in range(count)
        ]


@pytest.fixture
def make() -> type[Make]:
    """Return the builders for synthetic price and fundamental history."""
    return Make


class MemoryEvidenceStorage:
    """An `EvidenceStorage` that is a dict. No filesystem, no `Path`, no root.

    The point of this class is what it lacks. Any evidence-layer function
    that works against it provably needs nothing from a local directory,
    which is the seam a remote backend will later be plugged into. It is
    deliberately tiny — a mapping, key validation, and two testing hooks:
    `failing` names operations that raise `StorageUnavailableError`, so a
    test can show a backend fault stays loud; `listed` records every prefix
    handed to `list_prefix`, so a test can show a scoped audit stayed scoped.

    Reached through the `memory_storage` fixture rather than imported — see
    `Make` for why.
    """

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self._objects: dict[str, bytes] = {}
        self.failing = failing
        self.listed: list[str] = []

    def _check(self, operation: str) -> None:
        if operation in self.failing:
            raise StorageUnavailableError(f"memory backend: {operation} is unavailable")

    def exists(self, key: str) -> bool:
        """Whether the key was written."""
        validate_key(key)
        self._check("exists")
        return key in self._objects

    def stat(self, key: str) -> ArtifactStat:
        """Size of what was written under the key."""
        validate_key(key)
        self._check("stat")
        if key not in self._objects:
            raise ArtifactNotFoundError(key)
        return ArtifactStat(key=key, bytes=len(self._objects[key]))

    def read_bytes(self, key: str) -> bytes:
        """The bytes written under the key."""
        validate_key(key)
        self._check("read_bytes")
        if key not in self._objects:
            raise ArtifactNotFoundError(key)
        return self._objects[key]

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """Keep the bytes under the key. Never touches a manifest."""
        validate_key(key)
        if "write_bytes" in self.failing:
            raise EvidenceWriteError("memory backend: write_bytes did not complete")
        self._objects[key] = bytes(data)
        return StoredArtifact(key=key, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def digest(self, key: str) -> str:
        """SHA-256 of the bytes written under the key."""
        validate_key(key)
        self._check("digest")
        return hashlib.sha256(self.read_bytes(key)).hexdigest()

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """Every key strictly beneath the prefix, sorted; `""` lists everything."""
        if prefix:
            validate_key(prefix.removesuffix("/"))
        self._check("list_prefix")
        self.listed.append(prefix)
        prefix = prefix.removesuffix("/")
        yield from sorted(
            key for key in self._objects if not prefix or key.startswith(prefix + "/")
        )


@pytest.fixture
def memory_storage() -> type[MemoryEvidenceStorage]:
    """Return the in-memory `EvidenceStorage` used to prove backend neutrality."""
    return MemoryEvidenceStorage


def _memory_storage_satisfies_the_contract(storage: MemoryEvidenceStorage) -> EvidenceStorage:
    """Static proof, checked by mypy, that the fake is structurally a backend."""
    return storage


def s3_error(code: str, status: int, operation: str) -> ClientError:
    """A `ClientError` shaped the way botocore raises one for an S3 response."""
    response: dict[str, Any] = {
        "Error": {"Code": code, "Message": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)  # type: ignore[arg-type]  # stubs want every metadata key


class FakeS3Client:
    """A stateful stand-in for the S3 client `B2EvidenceStorage` drives.

    A dict of objects behind the four calls the backend makes — `head_object`,
    `get_object`, `put_object`, `get_paginator("list_objects_v2")` — answering
    with the same shapes botocore returns: a `StreamingBody` for a GET, a
    `ClientError` carrying `404` for a HEAD of nothing and `NoSuchKey` for a
    GET of nothing, pages with `Contents` and continuation. What it lacks is
    the point: no network, no credentials, no bucket.

    Three testing hooks. `failing` maps an operation name to the exception
    it should raise, so a test can show a 403, a 5xx or a connection fault
    stays loud. `calls` records every operation with its key, so a test can
    show an unsafe key issued no request and no delete was ever issued.
    `page_size` and `reverse_pages` make listing paginate and arrive out of
    order, so a test can show the backend sorts and deduplicates rather than
    trusting the server.

    Reached through the `fake_s3` fixture rather than imported — see `Make`.
    """

    def __init__(
        self,
        *,
        page_size: int = 1000,
        reverse_pages: bool = False,
        failing: dict[str, BaseException] | None = None,
    ) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.buckets_seen: set[str] = set()
        self.failing = failing or {}
        self.page_size = page_size
        self.reverse_pages = reverse_pages
        #: Set by a test to make the bucket "hold" different metadata or
        #: bytes than were uploaded — a corrupted or truncated upload.
        self.tamper_after_put: dict[str, tuple[bytes, dict[str, str]]] = {}

    def _call(self, operation: str, key: str, bucket: str) -> None:
        self.calls.append((operation, key))
        self.buckets_seen.add(bucket)
        if operation in self.failing:
            raise self.failing[operation]

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - SDK spelling
        """HEAD: size and metadata, or a bare 404."""
        self._call("head_object", Key, Bucket)
        if Key not in self.objects:
            raise s3_error("404", 404, "HeadObject")
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": dict(metadata), "ETag": '"not-a-sha"'}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - SDK spelling
        """GET: a streaming body, or NoSuchKey."""
        self._call("get_object", Key, Bucket)
        if Key not in self.objects:
            raise s3_error("NoSuchKey", 404, "GetObject")
        data, _ = self.objects[Key]
        return {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)}

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 - SDK spelling
        Key: str,  # noqa: N803 - SDK spelling
        Body: bytes,  # noqa: N803 - SDK spelling
        ContentLength: int,  # noqa: N803 - SDK spelling
        Metadata: Mapping[str, str],  # noqa: N803 - SDK spelling
    ) -> dict[str, Any]:
        """PUT: store bytes and metadata; ETag is deliberately not a digest."""
        self._call("put_object", Key, Bucket)
        assert len(Body) == ContentLength
        self.objects[Key] = (bytes(Body), dict(Metadata))
        if Key in self.tamper_after_put:
            self.objects[Key] = self.tamper_after_put[Key]
        return {"ETag": '"not-a-sha"'}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - SDK spelling
        """Never called by the backend; recorded so a test can prove it."""
        self._call("delete_object", Key, Bucket)
        raise AssertionError("the evidence backend must never delete")

    def delete_objects(
        self,
        *,
        Bucket: str,  # noqa: N803 - SDK spelling
        Delete: Mapping[str, Any],  # noqa: N803 - SDK spelling
    ) -> dict[str, Any]:
        """Batch delete — the canary cleanup's call, never the backend's."""
        keys = [str(item["Key"]) for item in Delete["Objects"]]
        self._call("delete_objects", ",".join(keys), Bucket)
        for key in keys:
            self.objects.pop(key, None)
        return {"Deleted": [{"Key": key} for key in keys]}

    def get_paginator(self, operation_name: str) -> FakeS3Paginator:
        """Only `list_objects_v2` is paged, as in the real client."""
        assert operation_name == "list_objects_v2"
        return FakeS3Paginator(self)


class FakeS3Paginator:
    """`list_objects_v2` pages over the fake's objects, sized and ordered by the test."""

    def __init__(self, client: FakeS3Client) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803 - SDK spelling
        client = self._client
        client._call("list_objects_v2", Prefix, Bucket)
        keys = sorted(k for k in client.objects if k.startswith(Prefix))
        if client.reverse_pages:
            keys = list(reversed(keys))
        if not keys:
            yield {"KeyCount": 0, "IsTruncated": False}
            return
        for start in range(0, len(keys), client.page_size):
            page = keys[start : start + client.page_size]
            yield {
                "Contents": [{"Key": k, "Size": len(client.objects[k][0])} for k in page],
                "KeyCount": len(page),
                "IsTruncated": start + client.page_size < len(keys),
            }


@pytest.fixture
def fake_s3() -> type[FakeS3Client]:
    """Return the in-memory S3 client used to test `B2EvidenceStorage` offline."""
    return FakeS3Client


def submissions_payload(cik: int, filings: list[tuple[str, str, str, str]]) -> bytes:
    """A submissions catalogue in the SEC's column-wise shape.

    `filings` are `(accession, form, filing date, primary document)`.
    """
    return json.dumps(
        {
            "cik": f"{cik:010d}",
            "filings": {
                "recent": {
                    "accessionNumber": [f[0] for f in filings],
                    "form": [f[1] for f in filings],
                    "filingDate": [f[2] for f in filings],
                    "reportDate": ["" for _ in filings],
                    "primaryDocument": [f[3] for f in filings],
                    "acceptanceDateTime": [f"{f[2]}T16:05:00.000Z" for f in filings],
                }
            },
        }
    ).encode()


class FakeSec:
    """A dict of URLs that answers like the SEC and records every fetch.

    The rebuild's `DocumentSource`: `fetch` returns the canned bytes for a
    URL with their digest, raises `ProviderRequestError` for an unknown one,
    and honours two hooks — `fail_once` (a transient failure on the first
    attempt) and `fail_always` (a refusal or a persistent fault). `calls`
    records every URL asked for, so a test can show the ticker map was
    fetched once, a refused request was the last one, or nothing at all was
    asked on a resume.

    Args:
        ticker_map: The SEC ticker map the fake serves, in the SEC's shape.
    """

    def __init__(self, ticker_map: Mapping[str, Mapping[str, object]] | None = None) -> None:
        self.responses: dict[str, bytes] = {
            TICKER_MAP_URL: json.dumps(dict(ticker_map or {})).encode()
        }
        self.calls: list[str] = []
        self.fail_once: dict[str, type[Exception]] = {}
        self.fail_always: dict[str, type[Exception]] = {}

    def add_filer(self, cik: int, docs: list[tuple[str, str, str, str]]) -> None:
        """Serve a filer's catalogue and one document per filing."""
        self.responses[f"https://data.sec.gov/submissions/CIK{cik:010d}.json"] = (
            submissions_payload(cik, docs)
        )
        for accession, _, _, document in docs:
            plain = accession.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{plain}/{document}"
            self.responses[url] = f"<html><body>{cik} {accession} {document}</body></html>".encode()

    def close(self) -> None:
        """The CLI closes its client; the fake has nothing to release."""

    def fetch(self, url: str) -> FetchedDocument:
        """Answer one URL, or fail the way the test arranged."""
        self.calls.append(url)
        if url in self.fail_always:
            raise self.fail_always[url]("sec refused")
        if url in self.fail_once:
            raise self.fail_once.pop(url)("transient")
        body = self.responses.get(url)
        if body is None:
            raise ProviderRequestError(f"404 {url}")
        return FetchedDocument(
            url=url,
            status=200,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            content_type="application/json" if url.endswith(".json") else "text/html",
            etag=None,
            last_modified=None,
            retrieved_at=datetime(2026, 9, 16, tzinfo=UTC),
        )


@pytest.fixture
def fake_sec() -> type[FakeSec]:
    """Return the canned SEC used to drive the rebuild offline."""
    return FakeSec


def identity_row(
    position: int, bucket: str, symbol: str, cik: int | None = None, *, chunk: int | None = None
) -> IdentityRow:
    """One row of a V2 identity plan; `chunk` defaults to the production arithmetic."""
    return IdentityRow(
        position=position,
        chunk=position // 250 if chunk is None else chunk,
        bucket=bucket,
        symbol=symbol,
        name=symbol,
        company_id=position if bucket == "LIVE" else None,
        historical_security_id=None if bucket == "LIVE" else position,
        cik=cik,
        resolution="STORED" if cik is not None else "EDGAR_REQUIRED",
        submissions_url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json" if cik else None,
    )


@pytest.fixture
def make_identity() -> Callable[..., IdentityRow]:
    """Return the identity-row builder for V2 plans."""
    return identity_row


@pytest.fixture
def eligible_profile() -> CompanyProfile:
    """A listing that clears every eligibility threshold."""
    return CompanyProfile(
        ticker="XYZ",
        name="Example Corp",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        market_cap=1_200_000_000.0,
    )


@pytest.fixture
def providers(eligible_profile: CompanyProfile) -> tuple[MockMarketData, MockFundamentals]:
    """Mock providers serving one eligible company with full history."""
    bars = Make.bars(sessions=60)
    periods = Make.quarters()
    return (
        MockMarketData([eligible_profile], {eligible_profile.ticker: bars}),
        MockFundamentals(
            {eligible_profile.ticker: eligible_profile}, {eligible_profile.ticker: periods}
        ),
    )


SCORE_DATE = date(2026, 6, 30)
"""The day every seeded score describes. Fixed so nothing depends on today."""


class Seed:
    """Writes companies, history and scores into a test database."""

    score_date = SCORE_DATE

    @staticmethod
    def company(
        session: Session,
        make: type[Make],
        ticker: str,
        *,
        name: str | None = None,
        quarters: int = 8,
        sessions: int = 60,
        revenue: float = 100_000_000.0,
    ) -> int:
        """Store one company with fundamentals and price history.

        Returns:
            The stored company's primary key.
        """
        profile = CompanyProfile(
            ticker=ticker,
            name=name or f"{ticker} Corporation",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            market_cap=1_200_000_000.0,
        )
        companies = CompanyRepository(session)
        companies.upsert_profile(profile)
        stored = companies.get_by_ticker(ticker)
        assert stored is not None

        FinancialSnapshotRepository(session).upsert_periods(
            stored.id, make.quarters(count=quarters, revenue=revenue)
        )
        PriceHistoryRepository(session).upsert_bars(stored.id, make.bars(sessions=sessions))
        session.flush()
        return stored.id

    @staticmethod
    def score(
        session: Session,
        company_id: int,
        ticker: str,
        *,
        final: float = 80.0,
        coverage: float = 1.0,
        growth: float = 30.0,
        quality: float = 20.0,
        valuation: float = 18.0,
        status: ScoringStatus = ScoringStatus.SCORED,
        score_date: date = SCORE_DATE,
        score_version: str = CURRENT_SCORE_VERSION,
        ranking_state: str = PRELIMINARY,
        market_cap: float | None = 1_200_000_000.0,
        revenue_growth: float | None = 0.35,
        market_cap_source: MarketCapSource = MarketCapSource.CALCULATED,
        liquidity_basis: VolumeBasis = VolumeBasis.PARTIAL,
    ) -> None:
        """Store one score snapshot, breakdown included."""
        ScoreSnapshotRepository(session).upsert_scores(
            [
                ScoreRecord(
                    company_id=company_id,
                    score=Seed.company_score(
                        ticker,
                        final=final,
                        coverage=coverage,
                        growth=growth,
                        quality=quality,
                        valuation=valuation,
                        status=status,
                        score_version=score_version,
                    ),
                    metrics=CompanyMetrics(
                        ticker=ticker,
                        market_cap=market_cap,
                        market_cap_source=market_cap_source,
                        liquidity_basis=liquidity_basis,
                        average_dollar_volume_20d=12_500_000.0,
                        revenue_growth_yoy=revenue_growth,
                        revenue_growth_acceleration=0.12,
                        enterprise_value=1_000_000_000.0,
                    ),
                    ranking_state=ranking_state,
                )
            ],
            score_date,
        )
        session.flush()

    @staticmethod
    def company_score(
        ticker: str,
        *,
        final: float = 80.0,
        coverage: float = 1.0,
        growth: float = 30.0,
        quality: float = 20.0,
        valuation: float = 18.0,
        status: ScoringStatus = ScoringStatus.SCORED,
        score_version: str = CURRENT_SCORE_VERSION,
    ) -> CompanyScore:
        """Build a complete score, including one unavailable sub-score.

        The unavailable sub-score is the point: a brief must carry the metrics
        the score could *not* use, and a fixture with perfect data would never
        exercise that.
        """
        if status is not ScoringStatus.SCORED:
            return CompanyScore(ticker=ticker, score_version=score_version, status=status)

        return CompanyScore(
            ticker=ticker,
            score_version=score_version,
            status=status,
            growth=_component("growth", "revenue_growth", growth, 35.0, coverage),
            quality=_component("quality", "gross_margin", quality, 25.0, coverage),
            valuation=_component("valuation", "valuation_multiple", valuation, 25.0, coverage),
            momentum=_component("momentum", "position_52w", 12.0, 15.0, coverage),
            raw_score=growth + quality + valuation + 12.0,
            risk=RiskAssessment(
                dilution_penalty=0.0,
                runway_penalty=None,
                balance_sheet_penalty=0.0,
                total_penalty=0.0,
                level=RiskLevel.LOW,
                coverage=0.67,
                share_count_growth_yoy=0.02,
                warnings=(ScoreWarning.RUNWAY_NOT_ASSESSED,),
            ),
            final_score=final,
            category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
            data_coverage=coverage,
            valuation_basis=ValuationBasis.EV_TO_REVENUE,
            warnings=(ScoreWarning.WEIGHT_REDISTRIBUTED,),
        )


def _component(
    name: str, primary: str, score: float, maximum: float, coverage: float
) -> ComponentScore:
    """Build a component with one scored and one unavailable sub-score.

    The scored one is named after a real sub-score the engine emits, so the
    brief's preserve map actually fires in these tests. The unavailable one is
    the point of the fixture: a brief must carry what the score could not judge.
    """
    return ComponentScore(
        name=name,
        status=ComponentStatus.SCORED,
        score=score,
        max_points=maximum,
        coverage=coverage,
        subscores=(
            SubScore(
                name=primary,
                points=score,
                max_points=maximum - 5.0,
                observed=0.35,
                unit=MetricUnit.PERCENT,
            ),
            SubScore(name=f"{name}_secondary", points=None, max_points=5.0, observed=None),
        ),
        redistributed=True,
    )


@pytest.fixture
def seed() -> type[Seed]:
    """Return the builders that put companies and scores in the database."""
    return Seed


@pytest.fixture
def scored_company(session: Session, make: type, seed: type[Seed]) -> str:
    """Store one fully scored company and return its ticker."""
    company_id = seed.company(session, make, "XYZ")
    seed.score(session, company_id, "XYZ")
    return "XYZ"
