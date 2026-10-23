"""The second generation starts from what survived, and from nothing else.

The first generation's corpus and manifest are gone. What is left is the
production database, the code, and the frozen digests. These tests pin what
the generation planner may conclude from those:

- the identity plan is the production universe in its own order, with a
  stored CIK where the database has one and an explicit `EDGAR_REQUIRED`
  where it does not — never a guessed filer, never a network call;
- the plan reproduces byte for byte, and its digests with it;
- the second generation begins at zero: an empty manifest is zero trusted
  artifacts, and a checkpoint that claims a finished batch over an empty
  manifest is reported as a contradiction, not counted;
- Plan A3 is `MISSING_PREREQUISITE`, with the missing inputs named, because
  the honest answer is the only one worth writing down;
- the bucket is listed and never written, and objects already under the
  artifact prefixes are reported, not adopted.

No network. The database is a temporary one seeded here; the bucket is the
fake client from `conftest`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import HistoricalSecurity
from domain import CompanyProfile
from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    A3_EXPECTED_SHA256,
    A3_EXPECTED_TARGETS,
    GENERATION_ID,
    A3Status,
    B2Configuration,
    B2EvidenceStorage,
    Checkpoint,
    ChunkStage,
    CikResolution,
    Entry,
    Manifest,
    Outcome,
    PrerequisiteStatus,
    build_generation_plan,
    identity_plan,
    open_control_root,
    production_universe,
    save_checkpoint,
    selection_digest,
    v2_progress,
    write_generation_plan,
)
from stock_screener.sec_evidence.binding import BindingError, read_only_url

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from conftest import FakeS3Client
    from stock_screener.sec_evidence import EvidenceControlRoot

pytestmark = pytest.mark.integration

B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="generation-test-bucket",
    key_id=SecretStr("generation-key-id"),
    application_key=SecretStr("generation-application-key"),
)


def _seed(session: Session) -> None:
    """Five live companies (no CIK column exists) and three delisted securities, two with CIKs."""
    for ticker in ("AAPL", "MSFT", "GOOGL", "BRKB", "T"):
        CompanyRepository(session).upsert_profile(
            CompanyProfile(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ")
        )
    session.add(
        HistoricalSecurity(primary_ticker="BBBY", name="Bed Bath", cik=886158, status="UNKNOWN")
    )
    session.add(
        HistoricalSecurity(primary_ticker="SGEN", name="Seagen", cik=1060736, status="UNKNOWN")
    )
    session.add(
        HistoricalSecurity(primary_ticker="ZZZZ", name="No Filer", cik=None, status="UNKNOWN")
    )
    session.flush()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "stage.db"
    engine = create_engine_from_url(f"sqlite:///{path}")
    create_all(engine)
    with session_scope(build_session_factory(engine)) as session:
        _seed(session)
    engine.dispose()
    return path


@pytest.fixture
def factory(database: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_from_url(read_only_url(database))
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def control(tmp_path: Path) -> EvidenceControlRoot:
    root = tmp_path / "control"
    root.mkdir()
    return open_control_root(Settings(sec_control_dir=str(root)))


class TestTheIdentityPlanIsTheUniverse:
    def test_rows_follow_the_universe_order_exactly(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            rows = identity_plan(session)
            universe = production_universe(session)

        assert [r.symbol for r in rows] == [s.symbol for s in universe]
        assert [r.position for r in rows] == list(range(len(universe)))
        assert all(r.chunk == r.position // 250 for r in rows)
        assert len(rows) == 8

    def test_a_stored_cik_yields_an_offline_submissions_request(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            rows = {r.symbol: r for r in identity_plan(session)}

        assert rows["BBBY"].resolution == CikResolution.STORED
        assert rows["BBBY"].cik == 886158
        assert rows["BBBY"].submissions_url == "https://data.sec.gov/submissions/CIK0000886158.json"

    def test_an_identity_without_a_stored_cik_is_marked_not_guessed(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            rows = {r.symbol: r for r in identity_plan(session)}

        for symbol in ("AAPL", "MSFT", "ZZZZ"):
            assert rows[symbol].resolution == CikResolution.EDGAR_REQUIRED
            assert rows[symbol].cik is None
            assert rows[symbol].submissions_url is None


class TestThePlanIsDeterministic:
    def test_two_builds_write_identical_plans_and_digests(
        self, factory: sessionmaker[Session], database: Path, control: EvidenceControlRoot
    ) -> None:
        with session_scope(factory) as session:
            first = build_generation_plan(session, database, control, expect_universe_count=8)
            paths = write_generation_plan(first, control)
            bytes_one = paths["identities"].read_bytes()
            targets_one = paths["offline_targets"].read_bytes()
            sha_one = paths["identities_sha256"].read_text()
            second = build_generation_plan(session, database, control, expect_universe_count=8)
            write_generation_plan(second, control)

        assert paths["identities"].read_bytes() == bytes_one
        assert paths["offline_targets"].read_bytes() == targets_one
        assert paths["identities_sha256"].read_text() == sha_one
        assert first.metadata.identity_plan_sha256 == second.metadata.identity_plan_sha256
        assert first.metadata.offline_targets_sha256 == second.metadata.offline_targets_sha256
        assert sha_one.split()[0] == first.metadata.identity_plan_sha256
        assert paths["identities"].is_relative_to(control.plans / "generation_v2")

    def test_the_universe_digest_is_recorded_and_enforced(
        self, factory: sessionmaker[Session], database: Path, control: EvidenceControlRoot
    ) -> None:
        with session_scope(factory) as session:
            expected = selection_digest(production_universe(session))
            plan = build_generation_plan(
                session, database, control, expect_universe_count=8, expect_universe_sha256=expected
            )
            with pytest.raises(BindingError, match="digest"):
                build_generation_plan(
                    session,
                    database,
                    control,
                    expect_universe_count=8,
                    expect_universe_sha256="0" * 64,
                )
            with pytest.raises(BindingError, match="expected 8919"):
                build_generation_plan(session, database, control)

        assert plan.metadata.universe_sha256 == expected
        assert plan.metadata.universe_count == 8


class TestTheSecondGenerationStartsAtZero:
    def test_an_empty_control_root_is_zero_trusted_and_zero_batches(
        self, control: EvidenceControlRoot
    ) -> None:
        assert v2_progress(control) == (0, 0, [])

    def test_a_first_generation_checkpoint_over_an_empty_manifest_is_a_contradiction(
        self, control: EvidenceControlRoot
    ) -> None:
        """Eighteen COMPLETE batches and 342 trusted targets do not carry over."""
        for offset in (0, 250, 4250):
            save_checkpoint(
                control.checkpoints / f"chunk-{offset:06d}-0250.json",
                Checkpoint(
                    offset=offset,
                    limit=250,
                    selection_digest="v1",
                    selected=250,
                    stage=ChunkStage.COMPLETE,
                    accounting={"trusted": 342},
                ),
            )

        trusted, completed, inconsistent = v2_progress(control)

        assert trusted == 0
        assert completed == 0
        assert inconsistent == [
            "chunk-000000-0250.json",
            "chunk-000250-0250.json",
            "chunk-004250-0250.json",
        ]

    def test_a_completed_batch_the_manifest_supports_counts(
        self, control: EvidenceControlRoot
    ) -> None:
        manifest = Manifest(control.manifest_path)
        manifest.append(
            Entry(
                url="https://data.sec.gov/submissions/CIK0000886158.json",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=2,
                path="submissions/CIK0000886158.json",
                request_kind="SUBMISSIONS",
                cik=886158,
            )
        )
        save_checkpoint(
            control.checkpoints / "chunk-000000-0250.json",
            Checkpoint(
                offset=0,
                limit=250,
                selection_digest="v2",
                selected=250,
                stage=ChunkStage.COMPLETE,
                accounting={"trusted": 1},
            ),
        )

        assert v2_progress(control) == (1, 1, [])

    def test_the_metadata_records_the_start_state_and_the_lost_generation(
        self, factory: sessionmaker[Session], database: Path, control: EvidenceControlRoot
    ) -> None:
        with session_scope(factory) as session:
            plan = build_generation_plan(session, database, control, expect_universe_count=8)

        meta = plan.metadata
        assert meta.generation_id == GENERATION_ID
        assert meta.previous_generation == "SEC_EVIDENCE_GENERATION_V1"
        assert meta.v2_trusted_artifacts == 0
        assert meta.v2_completed_batches == 0
        assert meta.baseline_plan_status == "PARTIAL"
        assert meta.identities_stored_cik == 2
        assert meta.identities_edgar_required == 6


class TestPlanA3IsHonestlyMissing:
    def test_the_status_names_the_frozen_target_and_the_missing_inputs(
        self, factory: sessionmaker[Session], database: Path, control: EvidenceControlRoot
    ) -> None:
        with session_scope(factory) as session:
            plan = build_generation_plan(session, database, control, expect_universe_count=8)

        assert plan.metadata.a3_status == A3Status.MISSING_PREREQUISITE
        assert plan.metadata.a3_expected_targets == A3_EXPECTED_TARGETS == 61_941
        assert plan.metadata.a3_expected_sha256 == A3_EXPECTED_SHA256
        statuses = {p.name: p.status for p in plan.prerequisites}
        assert statuses["derived evidence index (catalogue, filings, securities, cover rows)"] == (
            PrerequisiteStatus.MISSING
        )
        assert any("Plan A3 planning inputs" in name for name in statuses)
        assert any(s == PrerequisiteStatus.NETWORK for s in statuses.values())
        assert any(s == PrerequisiteStatus.SURVIVING for s in statuses.values())
        assert plan.metadata.missing_prerequisites, "the missing list is not empty"
        assert not any(
            "SURVIVING" in p.status
            for p in plan.prerequisites
            if p.name in plan.metadata.missing_prerequisites
        )


class TestTheBucketIsListedNeverWritten:
    def test_an_empty_bucket_costs_three_lists(
        self,
        factory: sessionmaker[Session],
        database: Path,
        control: EvidenceControlRoot,
        fake_s3: type[FakeS3Client],
    ) -> None:
        client = fake_s3()
        with session_scope(factory) as session:
            plan = build_generation_plan(
                session,
                database,
                control,
                expect_universe_count=8,
                remote=B2EvidenceStorage(B2_CONFIG, client=client),
            )

        assert plan.metadata.production_b2_objects == {
            "filings": 0,
            "submissions": 0,
            "fulltext": 0,
        }
        assert [op for op, _ in client.calls] == ["list_objects_v2"] * 3

    def test_objects_already_under_artifact_prefixes_are_reported_not_adopted(
        self,
        factory: sessionmaker[Session],
        database: Path,
        control: EvidenceControlRoot,
        fake_s3: type[FakeS3Client],
    ) -> None:
        client = fake_s3()
        client.objects["filings/0000886158/x/a.htm"] = (b"?", {})
        client.objects["_canary/b28-x/filings/0000886158/x/a.htm"] = (b"?", {})
        with session_scope(factory) as session:
            plan = build_generation_plan(
                session,
                database,
                control,
                expect_universe_count=8,
                remote=B2EvidenceStorage(B2_CONFIG, client=client),
            )

        assert plan.metadata.production_b2_objects == {
            "filings": 1,
            "submissions": 0,
            "fulltext": 0,
        }
        assert plan.metadata.v2_trusted_artifacts == 0, "a bucket object is not evidence"
        assert not any(
            op in {"put_object", "delete_object", "delete_objects"} for op, _ in client.calls
        )


class TestTheCommand:
    @pytest.fixture
    def environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        control = tmp_path / "control"
        control.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
        monkeypatch.setenv("B2_BUCKET", "generation-test-bucket")
        monkeypatch.setenv("B2_KEY_ID", "generation-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "generation-application-key")
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

    def test_the_plan_is_written_with_no_fetch_and_no_write(
        self,
        environment: Path,
        database: Path,
        fake_s3: type[FakeS3Client],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )

        def no_network(*args: object, **kwargs: object) -> object:
            raise AssertionError("the generation planner must not build a SEC client")

        monkeypatch.setattr("stock_screener.cli.build_observation_source", no_network)
        monkeypatch.setattr("stock_screener.cli._sec_documents", no_network)

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "generation-plan",
                "--database",
                str(database),
                "--expect-universe-count",
                "8",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "GENERATION PLAN WRITTEN — nothing was fetched" in result.stdout
        assert "Plan A3          : MISSING_PREREQUISITE" in result.stdout
        assert "V2 progress      : 0 trusted artifacts, 0 completed batches" in result.stdout
        generation = json.loads(
            (environment / "plans" / "generation_v2" / "generation.json").read_text()
        )
        assert generation["generation_id"] == GENERATION_ID
        assert generation["v2_trusted_artifacts"] == 0
        assert (environment / "plans" / "generation_v2" / "base_identities.sha256").is_file()
        assert "generation-application-key" not in result.stdout
        assert [op for op, _ in client.calls] == ["list_objects_v2"] * 3

    def test_a_non_empty_production_prefix_stops_the_command(
        self,
        environment: Path,
        database: Path,
        fake_s3: type[FakeS3Client],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = fake_s3()
        client.objects["submissions/CIK0000886158.json"] = (b"{}", {})
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "generation-plan",
                "--database",
                str(database),
                "--expect-universe-count",
                "8",
            ],
        )

        assert result.exit_code == 1
        assert "production prefixes are not empty" in result.stdout

    def test_the_wrong_database_is_refused(self, environment: Path, database: Path) -> None:
        result = CliRunner().invoke(
            app, ["evidence", "generation-plan", "--database", str(database), "--no-check-remote"]
        )

        assert result.exit_code == 1
        assert "expected 8919" in result.stdout
