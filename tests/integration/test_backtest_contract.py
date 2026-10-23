"""The backtest data contract and the leakage canaries that make it more than prose.

Two halves. The contract half: the files build deterministically from the same
bindings, the dataset fingerprints are stable over a small research database,
`load_contract` accepts its own bindings and refuses any altered one, and the
completion marker names every file it wrote. The canary half: the properties
the contract asserts are exercised against the real assembly code — a filing
accepted after the decision instant cannot change what a decision saw; a bar
after the decision date cannot change a feature but does change a label; the
label layer is not imported by any feature or scoring module; a reused ticker
does not join two securities; two classes under one CIK are not both credited
by a filing that names one; a delisted security stays in the historical
universe; UNKNOWN stays UNKNOWN; a restatement filed later is invisible earlier.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from data_access import (
    CompanyRepository,
    FundamentalObservationRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import (
    FilingAcceptance,
    FtdIdentityObservation,
    FundamentalObservation,
    HistoricalSecurity,
    PriceHistory,
    ResearchUniverseMonth,
    SecurityIdentifier,
)
from domain import CompanyProfile
from domain.metrics import total_return
from domain.outcomes import forward_returns
from stock_screener.backtest_contract import (
    CONTRACT_FILES,
    CONTRACT_VERSION,
    DATASET_ID,
    ContractBindings,
    ContractError,
    build_contract,
    fingerprint_dataset,
    load_contract,
    mirror_contract,
    readiness_matrix,
    run_manifest_schema,
    write_contract,
)
from stock_screener.backtest_contract.contract import LAYERS
from stock_screener.pit.decision_time import DecisionPoint, decision_instant
from stock_screener.pit.metrics import periods_as_of
from stock_screener.pit.prices import RepositoryPriceSource
from stock_screener.sec_evidence import (
    B2Configuration,
    B2EvidenceStorage,
    ControlMirror,
    build_reachability,
)
from stock_screener.sec_evidence.join import JoinCache, join_symbol
from stock_screener.sec_evidence.reachability import Reach

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session, sessionmaker

    from conftest import FakeS3Client

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


def bindings(**overrides: object) -> ContractBindings:
    base: dict[str, object] = {
        "dataset_id": DATASET_ID,
        "contract_version": CONTRACT_VERSION,
        "universe_sha256": "b8" * 32,
        "evidence_manifest_sha256": "a5" * 32,
        "post_a3_index_sha256": "e6" * 32,
        "pit_marker_sha256": "e5" * 32,
        "planning_inputs_sha256": "79" * 32,
        "a3_plan_sha256": "0e" * 32,
        "pit_audit_marker_sha256": "ae" * 32,
        "database_sha256": "20" * 32,
        "fingerprints": {"historical_universe": {"rows": 1, "sha256": "11" * 32}},
    }
    base.update(overrides)
    return ContractBindings(**base)  # type: ignore[arg-type]


PIT_SUMMARY = {
    "pre_open": 242269,
    "newly_resolved": 155620,
    "still_open": 86649,
    "regressed": 0,
    "expected_solved": 155114.479,
    "actual_minus_expected": 505.521,
    "future_invalid_edges": 0,
    "predicted_solvable": 164572,
    "predicted_unsolvable": 77697,
    "fingerprints": {"month_outcomes": {"rows": 242269, "sha256": "a9" * 32}},
}
AUDIT_SUMMARY = {"sufficiency": {"SEC_MORE_MAY_HELP": 8487, "IDENTITY_DATA_REQUIRED": 41004}}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'research.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    try:
        yield maker
    finally:
        engine.dispose()


class TestTheContractFiles:
    def test_the_contract_builds_deterministically_and_names_every_file(
        self, tmp_path: Path
    ) -> None:
        first = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        second = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )

        digests = write_contract(tmp_path / "c", first)

        assert first == second
        assert set(digests) == set(CONTRACT_FILES)
        marker = json.loads((tmp_path / "c" / "backtest_contract_complete.json").read_text())
        assert marker["stage"] == "BACKTEST_CONTRACT_COMPLETE"
        assert marker["files"] == {k: v for k, v in digests.items() if k != marker_name()}
        assert marker["sec_evidence_acquisition"] == "FROZEN"
        contract = first["backtest_data_contract_v1.json"]
        assert contract["dataset_id"] == DATASET_ID and contract["evidence_generation"] == (
            "SEC_EVIDENCE_GENERATION_V2"
        )
        assert contract["acquisition_status"]["sec_evidence_acquisition"] == "FROZEN"  # type: ignore[index]
        for name, sha in digests.items():
            assert hashlib.sha256((tmp_path / "c" / name).read_bytes()).hexdigest() == sha

    def test_the_contract_loads_under_its_bindings_and_refuses_any_other(
        self, tmp_path: Path
    ) -> None:
        files = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        write_contract(tmp_path / "c", files)

        loaded = load_contract(tmp_path / "c", expect=bindings())

        assert loaded["contract_version"] == CONTRACT_VERSION
        for field_name in (
            "universe_sha256",
            "evidence_manifest_sha256",
            "post_a3_index_sha256",
            "pit_marker_sha256",
            "database_sha256",
            "contract_version",
        ):
            with pytest.raises(ContractError, match="bound to other inputs"):
                load_contract(tmp_path / "c", expect=bindings(**{field_name: "00" * 32}))
        with pytest.raises(ContractError, match="bound to other inputs"):
            load_contract(
                tmp_path / "c",
                expect=bindings(
                    fingerprints={"historical_universe": {"rows": 2, "sha256": "22" * 32}}
                ),
            )

    def test_a_tampered_contract_file_is_refused_by_its_marker(self, tmp_path: Path) -> None:
        files = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        write_contract(tmp_path / "c", files)
        target = tmp_path / "c" / "historical_time_semantics.json"
        target.write_text(target.read_text().replace("16:00:00", "15:30:00"))

        with pytest.raises(ContractError, match="does not hash"):
            load_contract(tmp_path / "c", expect=bindings())

    def test_the_readiness_matrix_names_a_fix_for_every_blocker(self) -> None:
        matrix = readiness_matrix()

        assert {m["status"] for m in matrix} <= {
            "READY",
            "READY_WITH_DOCUMENTED_LIMITATION",
            "BLOCKED",
            "PARTIAL",
            "NOT_REQUIRED_FOR_FIRST_BASELINE",
        }
        for row in matrix:
            assert row["why"] and row["impact"] and row["minimum_fix"]
        assert [m["subsystem"] for m in matrix if m["status"] == "BLOCKED"] == ["sector_industry"]

    def test_the_run_manifest_schema_requires_the_bindings_and_the_selection_population(
        self,
    ) -> None:
        schema = run_manifest_schema()

        required_raw = schema["required"]
        assert isinstance(required_raw, list)
        required = set(required_raw)
        assert {
            "dataset_id",
            "contract_version",
            "universe_fingerprint",
            "price_data_fingerprint",
            "strategy_version",
            "execution_rule",
            "transaction_cost_assumptions",
            "variants_tried",
            "selection_criterion",
            "benchmark",
            "output_fingerprints",
        } <= required
        properties = schema["properties"]
        assert isinstance(properties, dict)
        assert properties["dataset_id"] == {"const": DATASET_ID}
        assert properties["execution_rule"]["enum"] == ["CLOSE_SIGNAL_NEXT_SESSION_OPEN"]

    def test_fingerprints_are_canonical_and_cover_every_layer(
        self, world: sessionmaker[Session], tmp_path: Path
    ) -> None:
        with session_scope(world) as session:
            company = CompanyRepository(session)
            company.upsert_profile(CompanyProfile(ticker="AAA", name="AAA Inc.", exchange="NASDAQ"))
            session.flush()
            cid = int(next(iter(company.list_all())).id)
            session.add(
                PriceHistory(
                    company_id=cid, date=date(2024, 1, 2), open=1, high=1, low=1, close=1, volume=10
                )
            )
            session.add(
                PriceHistory(
                    company_id=cid, date=date(2024, 1, 3), open=2, high=2, low=2, close=2, volume=10
                )
            )
        with closing(sqlite3.connect(f"file:{tmp_path / 'research.db'}?mode=ro", uri=True)) as db:
            first = fingerprint_dataset(db)
            second = fingerprint_dataset(db)

        assert first == second
        assert first["price_history"]["rows"] == 2
        assert set(first) == {name for names in LAYERS.values() for name in names}
        assert first["forward_return_labels"]["rows"] == 0

    def test_the_files_mirror_and_read_back(
        self, tmp_path: Path, fake_s3: type[FakeS3Client]
    ) -> None:
        files = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        write_contract(tmp_path / "c", files)
        client = fake_s3()
        mirror = ControlMirror(
            B2EvidenceStorage(
                B2Configuration(
                    endpoint_url="https://s3.us-west-004.backblazeb2.com",
                    bucket="contract-bucket",
                    key_id=SecretStr("contract-key-id"),
                    application_key=SecretStr("contract-application-key"),
                ),
                client=client,
            ),
            "SEC_EVIDENCE_GENERATION_V2",
        )

        records = mirror_contract(mirror, tmp_path / "c", generation_id="G")

        assert len(records) == len(CONTRACT_FILES)
        assert all(
            k.startswith("control/SEC_EVIDENCE_GENERATION_V2/backtest-contract/")
            for k in client.objects
        )


def marker_name() -> str:
    return "backtest_contract_complete.json"


# ----------------------------------------------------------------------------
# Canaries
# ----------------------------------------------------------------------------


def _observation(
    cid: int, concept: str, value: float, period_end: date, filed: date, accession: str
) -> FundamentalObservation:
    return FundamentalObservation(
        company_id=cid,
        observation_fingerprint=hashlib.sha256(
            f"{concept}{value}{period_end}{filed}{accession}".encode()
        ).hexdigest(),
        taxonomy="us-gaap",
        concept=concept,
        value=value,
        unit="USD",
        period_start=date(period_end.year, 1, 1),
        period_end=period_end,
        filed_date=filed,
        accession=accession,
        form="10-K",
        fiscal_year=period_end.year,
        fiscal_period="FY",
        frame=None,
        source="test",
    )


def _acceptance(accession: str, accepted: datetime, filed: date) -> FilingAcceptance:
    return FilingAcceptance(
        accession=accession, accepted_at=accepted, filed_date=filed, form="10-K", source="test"
    )


class TestFutureFilingCanary:
    def test_a_filing_accepted_after_the_decision_instant_cannot_change_what_the_decision_saw(
        self, world: sessionmaker[Session]
    ) -> None:
        as_of = date(2024, 3, 15)
        with session_scope(world) as session:
            repo = CompanyRepository(session)
            repo.upsert_profile(CompanyProfile(ticker="AAA", name="AAA Inc.", exchange="NASDAQ"))
            session.flush()
            cid = int(next(iter(repo.list_all())).id)
            session.add(
                _observation(cid, "Revenues", 100.0, date(2023, 12, 31), date(2024, 2, 20), "A-1")
            )
            session.add(
                _acceptance("A-1", datetime(2024, 2, 20, 21, 0, tzinfo=UTC), date(2024, 2, 20))
            )
            session.flush()
            before = [
                (o.concept, o.value)
                for o in FundamentalObservationRepository(session).available_as_of(
                    cid, as_of, accepted_by=decision_instant(as_of)
                )
            ]
            # Same-day filings: one accepted before the close, one after it.
            session.add(_observation(cid, "Revenues", 150.0, date(2023, 12, 31), as_of, "A-2"))
            # 15:55 America/New_York on 2024-03-15 (EDT) is 19:55 UTC.
            session.add(_acceptance("A-2", datetime(2024, 3, 15, 19, 55, tzinfo=UTC), as_of))
            session.add(_observation(cid, "Revenues", 200.0, date(2023, 12, 31), as_of, "A-3"))
            session.add(_acceptance("A-3", datetime(2024, 3, 15, 21, 30, tzinfo=UTC), as_of))
            # And a filing after the decision date entirely.
            session.add(
                _observation(cid, "Revenues", 300.0, date(2023, 12, 31), date(2024, 3, 20), "A-4")
            )
            session.add(
                _acceptance("A-4", datetime(2024, 3, 20, 21, 0, tzinfo=UTC), date(2024, 3, 20))
            )
            session.flush()
            after = [
                (o.concept, o.value)
                for o in FundamentalObservationRepository(session).available_as_of(
                    cid, as_of, accepted_by=decision_instant(as_of)
                )
            ]
            at_open = [
                o.value
                for o in FundamentalObservationRepository(session).available_as_of(
                    cid, as_of, accepted_by=decision_instant(as_of, DecisionPoint.OPEN)
                )
            ]
            next_day = [
                o.value
                for o in FundamentalObservationRepository(session).available_as_of(
                    cid, date(2024, 3, 16), accepted_by=decision_instant(date(2024, 3, 16))
                )
            ]

        assert before == [("Revenues", 100.0)]
        # 15:55 ET (19:55 UTC) is before the close and is admitted; 17:30 ET is not; 3-20 is not.
        assert sorted(v for _, v in after) == [100.0, 150.0]
        assert at_open == [100.0]
        assert sorted(next_day) == [100.0, 150.0, 200.0]


class TestRestatementCanary:
    def test_a_later_restatement_is_invisible_to_an_earlier_decision(
        self, world: sessionmaker[Session]
    ) -> None:
        with session_scope(world) as session:
            repo = CompanyRepository(session)
            repo.upsert_profile(CompanyProfile(ticker="AAA", name="AAA Inc.", exchange="NASDAQ"))
            session.flush()
            cid = int(next(iter(repo.list_all())).id)
            session.add(
                _observation(cid, "Revenues", 100.0, date(2023, 12, 31), date(2024, 2, 20), "R-1")
            )
            session.add(
                _acceptance("R-1", datetime(2024, 2, 20, 21, 0, tzinfo=UTC), date(2024, 2, 20))
            )
            session.add(
                _observation(cid, "Revenues", 90.0, date(2023, 12, 31), date(2024, 8, 1), "R-2")
            )
            session.add(
                _acceptance("R-2", datetime(2024, 8, 1, 21, 0, tzinfo=UTC), date(2024, 8, 1))
            )
            session.flush()
            repository = FundamentalObservationRepository(session)
            early = [
                o.value
                for o in repository.available_as_of(
                    cid, date(2024, 5, 1), accepted_by=decision_instant(date(2024, 5, 1))
                )
            ]
            late = sorted(
                o.value
                for o in repository.available_as_of(
                    cid, date(2024, 9, 1), accepted_by=decision_instant(date(2024, 9, 1))
                )
            )
            periods_early = periods_as_of(session, cid, date(2024, 5, 1))

        assert early == [100.0]
        assert late == [90.0, 100.0]
        assert all(p.period_end <= date(2024, 5, 1) for p in periods_early)


class TestFuturePriceCanary:
    def test_a_bar_after_the_decision_date_changes_the_label_and_not_the_feature(
        self, world: sessionmaker[Session]
    ) -> None:
        sessions = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10)]
        with session_scope(world) as session:
            repo = CompanyRepository(session)
            repo.upsert_profile(CompanyProfile(ticker="AAA", name="AAA Inc.", exchange="NASDAQ"))
            session.flush()
            cid = int(next(iter(repo.list_all())).id)
            for i, day in enumerate(sessions[:4]):
                px = 10.0 + i
                session.add(
                    PriceHistory(
                        company_id=cid, date=day, open=px, high=px, low=px, close=px, volume=100
                    )
                )
            session.flush()
            source = RepositoryPriceSource(session)
            as_of = sessions[3]
            bars_before = source.bars_until(cid, as_of)
            feature_before = total_return(bars_before, 2)
            # The future arrives: two more sessions, the second one a crash.
            session.add(
                PriceHistory(
                    company_id=cid,
                    date=sessions[4],
                    open=14.0,
                    high=14,
                    low=14,
                    close=14,
                    volume=100,
                )
            )
            session.add(
                PriceHistory(
                    company_id=cid, date=sessions[5], open=7.0, high=7, low=7, close=7, volume=100
                )
            )
            session.flush()
            bars_after = source.bars_until(cid, as_of)
            feature_after = total_return(bars_after, 2)
            all_bars = source.bars_until(cid, None)

        opens = {b.date: b.open for b in all_bars}
        closes = {b.date: b.close for b in all_bars}
        labels = forward_returns(sessions, opens, closes, as_of, horizons=(1,))
        crashed = forward_returns(
            sessions, {**opens, sessions[5]: 3.5}, closes, as_of, horizons=(1,)
        )

        assert [b.date for b in bars_after] == [b.date for b in bars_before] == sessions[:4]
        assert feature_after == feature_before
        assert labels[0].entry_date == sessions[4] and labels[0].exit_date == sessions[5]
        assert labels[0].total_return == pytest.approx(7.0 / 14.0 - 1)
        assert crashed[0].total_return == pytest.approx(3.5 / 14.0 - 1)


class TestForwardLabelCanary:
    FORBIDDEN_IMPORTERS = (
        "packages/domain/src/domain/scoring.py",
        "packages/domain/src/domain/signals.py",
        "packages/domain/src/domain/metrics.py",
        "packages/domain/src/domain/eligibility.py",
        "src/stock_screener/scoring/__init__.py",
        "src/stock_screener/signals/derivation.py",
        "src/stock_screener/pit/metrics.py",
        "src/stock_screener/pit/capture.py",
        "src/stock_screener/backtest/scores.py",
        "src/stock_screener/backtest/historical.py",
    )
    LABEL_TOKENS = (
        "domain.outcomes",
        "ForwardReturnRepository",
        "forward_returns",
        "ForwardReturn",
    )

    @pytest.mark.parametrize("relative", FORBIDDEN_IMPORTERS)
    def test_no_feature_or_scoring_module_imports_the_label_layer(self, relative: str) -> None:
        path = REPO / relative
        if not path.is_file():
            pytest.skip(f"{relative} is not present in this checkout")
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)

        assert not [name for name in imported if any(t in name for t in self.LABEL_TOKENS)], (
            relative
        )

    def test_the_label_fields_named_by_the_contract_are_not_feature_inputs(self) -> None:
        files = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        separation = files["feature_label_separation.json"]

        labels, importers = separation["label_fields"], separation["forbidden_importers_of_labels"]
        assert isinstance(labels, list) and isinstance(importers, list)
        assert "forward_returns.total_return" in labels
        assert {"domain.scoring", "domain.signals"} <= set(importers)


class TestTickerReuseCanary:
    def test_two_occupants_of_one_symbol_never_join_to_each_other(
        self, world: sessionmaker[Session]
    ) -> None:
        with session_scope(world) as session:
            repo = CompanyRepository(session)
            repo.upsert_profile(CompanyProfile(ticker="BBBY", name="Beyond, Inc.", exchange="NYSE"))
            session.flush()
            live_id = int(next(iter(repo.list_all())).id)
            old = HistoricalSecurity(
                primary_ticker="BBBY", name="Bed Bath & Beyond", cik=886158, status="BANKRUPT"
            )
            session.add(old)
            session.flush()
            for cusip, name, first, last in (
                ("075896100", "BED BATH & BEYOND INC", date(2004, 3, 24), date(2023, 5, 3)),
                ("690370101", "BEYOND, INC", date(2025, 9, 2), date(2026, 8, 12)),
            ):
                session.add(
                    FtdIdentityObservation(
                        cusip=cusip,
                        issuer_base=cusip[:6],
                        symbol="BBBY",
                        issuer_name=name,
                        first_settlement_date=first,
                        last_settlement_date=last,
                        observation_count=100,
                        cusip_valid=True,
                        first_source_file="f.zip",
                        last_source_file="f.zip",
                        evidence_hash=f"{cusip}{first}",
                    )
                )
            session.add(
                SecurityIdentifier(
                    historical_security_id=old.id,
                    cusip="075896100",
                    ticker="BBBY",
                    valid_from=date(2004, 3, 24),
                    valid_to=date(2023, 5, 3),
                    source="SEC_FTD",
                    confidence="OBSERVED",
                    lineage="INITIAL",
                    issuer_name="BBBY",
                )
            )
            session.flush()
            cache = JoinCache.load(session)
            in_2022 = join_symbol(session, "BBBY", date(2022, 6, 30), cache)
            in_2026 = join_symbol(session, "BBBY", date(2026, 1, 31), cache)
            in_gap = join_symbol(session, "BBBY", date(2024, 6, 30), cache)

        assert (in_2022.historical_security_id, in_2022.company_id) == (old.id, None)
        assert (in_2026.company_id, in_2026.historical_security_id) == (live_id, None)
        assert in_gap.company_id != live_id or str(in_gap.state) != "EXACT"


class TestCikClassCanary:
    def test_two_classes_under_one_cik_are_not_both_credited_by_a_filing_naming_one(self) -> None:
        common = ("COMPANY", 1)
        bond = ("COMPANY", 2)

        model = build_reachability({400: [common, bond]}, [(400, common)], [(400, bond)], [])

        reach = model.by_cik[400]
        assert reach.kind is Reach.SAFE_COMMON_CLASS
        assert reach.credited == frozenset({common})
        assert bond in reach.excluded_non_common
        assert model.credited(400) == frozenset({common})


class TestDelistedAndMissingnessCanaries:
    def test_a_delisted_security_keeps_its_historical_months_without_a_companies_row(
        self, world: sessionmaker[Session]
    ) -> None:
        with session_scope(world) as session:
            gone = HistoricalSecurity(
                primary_ticker="GONE", name="Gone Corp", cik=None, status="ACQUIRED"
            )
            session.add(gone)
            session.flush()
            for month in (date(2024, 1, 1), date(2024, 2, 1)):
                session.add(
                    ResearchUniverseMonth(
                        company_id=None,
                        historical_security_id=gone.id,
                        symbol="GONE",
                        observation_month=month,
                        venue="NASDAQ",
                        state="UNKNOWN",
                        security_class="UNKNOWN",
                        class_evidence_mode="ABSENT",
                        class_age_days=None,
                        class_source_accession=None,
                        class_evidence_as_of=None,
                        reason="fixture",
                    )
                )
            session.flush()
            from sqlalchemy import select

            rows = session.scalars(
                select(ResearchUniverseMonth).where(
                    ResearchUniverseMonth.historical_security_id == gone.id
                )
            ).all()
            companies = CompanyRepository(session).list_all()

        assert len(rows) == 2 and not companies
        assert all(r.company_id is None and r.state == "UNKNOWN" for r in rows)

    def test_unknown_stays_unknown_and_missing_stays_none(self) -> None:
        files = build_contract(
            bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        missingness = files["backtest_data_contract_v1.json"]["missingness"]

        assert isinstance(missingness, dict)
        assert "fillna(0)" in str(missingness["forbidden"])
        assert "UNKNOWN is not ineligible" in str(missingness["universe_verdict"])
        assert total_return([], 5) is None


def test_bindings_differences_name_every_moved_field() -> None:
    base = bindings()

    moved = replace(base, universe_sha256="00" * 32, database_sha256="11" * 32)

    assert base.differences(moved) == ["universe_sha256", "database_sha256"]
    assert base.differences(base) == []
