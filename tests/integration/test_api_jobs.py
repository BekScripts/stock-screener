"""The endpoints that start pipeline commands.

Two questions run through these. Can a caller only run the commands the
dashboard offers — never an arbitrary one? And does a second press of a button
report the run already in flight rather than starting a competing one?

No real process is spawned: the runner is built with a stub spawner, the same
way `test_jobs.py` does it. What is exercised here is the HTTP contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from data_access import JOB_RUNNING, JobRepository
from stock_screener.api import app, get_runner, get_session
from stock_screener.config import Settings
from stock_screener.jobs import JobRunner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session


class StubProcess:
    """A child that never existed, whose exit code the test decides."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


class StubSpawner:
    """Records what would have been run and hands back a controllable child."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.processes: list[StubProcess] = []

    def __call__(self, argv: list[str], log_path: Path) -> StubProcess:
        self.calls.append(argv)
        log_path.write_text("universe: processed=5752\n", encoding="utf-8")
        process = StubProcess(pid=4242 + len(self.processes))
        self.processes.append(process)
        return process


@pytest.fixture
def spawner() -> StubSpawner:
    return StubSpawner()


@pytest.fixture
def runner(tmp_path: Path, spawner: StubSpawner) -> JobRunner:
    return JobRunner(Settings(job_log_dir=str(tmp_path / "jobs")), spawn=spawner)


@pytest.fixture
def client(session: Session, runner: JobRunner) -> Iterator[TestClient]:
    """Yield a test client wired to the in-memory session and the stub runner."""
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_runner] = lambda: runner
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.integration
def test_starting_a_job_is_accepted(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"kind": "score"})

    assert response.status_code == 202
    assert response.json()["status"] == JOB_RUNNING
    assert response.json()["kind"] == "score"


@pytest.mark.integration
def test_starting_a_job_spawns_the_matching_command(
    client: TestClient, spawner: StubSpawner
) -> None:
    client.post("/api/jobs", json={"kind": "daily"})

    assert spawner.calls[0][1:] == ["-m", "stock_screener", "run-daily"]


@pytest.mark.integration
def test_an_unknown_kind_is_rejected(client: TestClient, spawner: StubSpawner) -> None:
    response = client.post("/api/jobs", json={"kind": "sh"})

    assert response.status_code == 400
    assert spawner.calls == []


@pytest.mark.integration
def test_a_ticker_cannot_smuggle_extra_arguments(client: TestClient, spawner: StubSpawner) -> None:
    response = client.post("/api/jobs", json={"kind": "research", "target": "NVDA --force"})

    assert response.status_code == 400
    assert spawner.calls == []


@pytest.mark.integration
def test_a_market_wide_kind_rejects_a_ticker(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"kind": "daily", "target": "NVDA"})

    assert response.status_code == 400


@pytest.mark.integration
def test_research_without_a_ticker_is_rejected(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"kind": "research"})

    assert response.status_code == 400


@pytest.mark.integration
def test_a_second_press_reports_the_run_already_in_flight(client: TestClient) -> None:
    first = client.post("/api/jobs", json={"kind": "daily"}).json()

    response = client.post("/api/jobs", json={"kind": "daily"})

    assert response.status_code == 409
    assert response.json()["detail"]["job"]["id"] == first["id"]


@pytest.mark.integration
def test_a_finished_job_frees_the_button(client: TestClient, spawner: StubSpawner) -> None:
    client.post("/api/jobs", json={"kind": "score"})
    spawner.processes[0].exit_code = 0

    response = client.post("/api/jobs", json={"kind": "score"})

    assert response.status_code == 202


@pytest.mark.integration
def test_listing_jobs_reports_what_is_running(client: TestClient) -> None:
    client.post("/api/jobs", json={"kind": "daily"})

    body = client.get("/api/jobs").json()

    assert [job["kind"] for job in body["running"]] == ["daily"]
    assert [job["kind"] for job in body["recent"]] == ["daily"]


@pytest.mark.integration
def test_listing_reconciles_a_process_that_has_exited(
    client: TestClient, spawner: StubSpawner
) -> None:
    client.post("/api/jobs", json={"kind": "score"})
    spawner.processes[0].exit_code = 0

    body = client.get("/api/jobs").json()

    assert body["running"] == []
    assert body["recent"][0]["status"] == "SUCCEEDED"


@pytest.mark.integration
def test_one_job_carries_the_tail_of_its_log(client: TestClient) -> None:
    started = client.post("/api/jobs", json={"kind": "daily"}).json()

    body = client.get(f"/api/jobs/{started['id']}").json()

    assert "universe: processed=5752" in body["log"]


@pytest.mark.integration
def test_an_unknown_job_id_is_a_404(client: TestClient) -> None:
    assert client.get("/api/jobs/9999").status_code == 404


@pytest.mark.integration
def test_the_kinds_endpoint_lists_what_may_be_run(client: TestClient) -> None:
    kinds = {entry["kind"]: entry for entry in client.get("/api/jobs/kinds").json()["kinds"]}

    assert kinds["research"]["needs_target"] is True
    assert kinds["research"]["spends_money"] is True
    assert kinds["score"]["spends_money"] is False


@pytest.mark.integration
def test_every_job_endpoint_is_absent_when_jobs_are_disabled(
    session: Session, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_screener import api

    monkeypatch.setattr(api, "get_settings", lambda: Settings(jobs_enabled=False))
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_runner] = lambda: runner

    with TestClient(app) as disabled:
        assert disabled.post("/api/jobs", json={"kind": "score"}).status_code == 404
        assert disabled.get("/api/jobs").status_code == 404
        assert disabled.get("/api/jobs/kinds").status_code == 404

    app.dependency_overrides.clear()
    assert JobRepository(session).recent() == []
