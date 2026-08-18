"""Job runner tests, run against a real SQLite database.

`integration` rather than `unit`: the concurrency guard is a query, and a test
with a mocked session would prove nothing about whether a second press finds the
first job.

No real process is ever spawned. `JobRunner` takes its spawner as an argument
precisely so these can substitute a stub — forking an interpreter per case would
make the suite slow and the exit codes untestable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_access import (
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_UNKNOWN,
    JobRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from stock_screener.config import Settings
from stock_screener.jobs import (
    JOB_KINDS,
    JobAlreadyRunningError,
    JobRunner,
    TargetRequiredError,
    UnknownJobKindError,
    normalise_target,
)

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
        log_path.write_text("started\n", encoding="utf-8")
        process = StubProcess(pid=4242 + len(self.processes))
        self.processes.append(process)
        return process


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


@pytest.fixture
def spawner() -> StubSpawner:
    return StubSpawner()


@pytest.fixture
def runner(tmp_path: Path, spawner: StubSpawner) -> JobRunner:
    settings = Settings(job_log_dir=str(tmp_path / "jobs"))
    return JobRunner(settings, spawn=spawner)


@pytest.mark.integration
def test_starting_a_job_records_it_as_running(session: Session, runner: JobRunner) -> None:
    record = runner.start(session, "score")

    assert record.status == JOB_RUNNING
    assert record.kind == "score"
    assert record.pid is not None


@pytest.mark.integration
def test_the_spawned_command_is_the_matching_cli_command(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    runner.start(session, "daily")

    assert spawner.calls[0][1:] == ["-m", "stock_screener", "run-daily"]


@pytest.mark.integration
def test_a_per_company_job_passes_the_ticker(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    runner.start(session, "research", target="nvda")

    assert spawner.calls[0][-1] == "NVDA"


@pytest.mark.integration
def test_research_prepares_filing_evidence_before_it_generates(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    # A company nobody has ingested filings for would otherwise be researched
    # anyway, and pay full price for a report whose filing-dependent sections
    # can only answer UNKNOWN.
    runner.start(session, "research", target="NVDA")

    assert spawner.calls[0][1:] == [
        "-m",
        "stock_screener",
        "research",
        "run",
        "--prepare",
        "NVDA",
    ]


@pytest.mark.integration
def test_an_unknown_kind_is_rejected(session: Session, runner: JobRunner) -> None:
    with pytest.raises(UnknownJobKindError, match="unknown job: rm-rf"):
        runner.start(session, "rm-rf")


@pytest.mark.integration
def test_a_second_identical_job_is_refused_while_the_first_runs(
    session: Session, runner: JobRunner
) -> None:
    first = runner.start(session, "daily")

    with pytest.raises(JobAlreadyRunningError) as caught:
        runner.start(session, "daily")

    assert caught.value.running.id == first.id


@pytest.mark.integration
def test_a_different_pipeline_kind_is_refused_while_one_runs(
    session: Session, runner: JobRunner
) -> None:
    # Guarding each kind separately let `scan` and `score` write the same rows
    # at once. They are different commands over one database, not independent
    # work, so only one pipeline job runs at a time.
    first = runner.start(session, "score")

    with pytest.raises(JobAlreadyRunningError) as caught:
        runner.start(session, "scan")

    assert caught.value.running.id == first.id
    assert caught.value.running.kind == "score"


@pytest.mark.integration
def test_research_runs_alongside_a_pipeline_job(session: Session, runner: JobRunner) -> None:
    # Research reads a snapshot that is already stored and writes only its own
    # company's report, so it neither blocks a pipeline run nor waits for one.
    runner.start(session, "score")

    research = runner.start(session, "research", target="NVDA")

    assert research.status == JOB_RUNNING


@pytest.mark.integration
def test_a_pipeline_job_runs_while_research_is_going(session: Session, runner: JobRunner) -> None:
    runner.start(session, "research", target="NVDA")

    pipeline = runner.start(session, "score")

    assert pipeline.status == JOB_RUNNING


@pytest.mark.integration
def test_a_second_research_job_for_the_same_ticker_is_refused(
    session: Session, runner: JobRunner
) -> None:
    first = runner.start(session, "research", target="NVDA")

    with pytest.raises(JobAlreadyRunningError) as caught:
        runner.start(session, "research", target="nvda")

    assert caught.value.running.id == first.id


@pytest.mark.integration
def test_research_on_one_company_does_not_block_another(
    session: Session, runner: JobRunner
) -> None:
    runner.start(session, "research", target="NVDA")

    second = runner.start(session, "research", target="AMD")

    assert second.status == JOB_RUNNING


@pytest.mark.integration
def test_a_finished_child_is_reconciled_to_succeeded(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    record = runner.start(session, "score")
    spawner.processes[0].exit_code = 0

    runner.reconcile(session)

    assert JobRepository(session).get(record.id) is not None
    assert record.status == JOB_SUCCEEDED
    assert record.exit_code == 0
    assert record.finished_at is not None


@pytest.mark.integration
def test_a_child_that_exited_non_zero_is_reconciled_to_failed(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    record = runner.start(session, "score")
    spawner.processes[0].exit_code = 2

    runner.reconcile(session)

    assert record.status == JOB_FAILED
    assert record.exit_code == 2


@pytest.mark.integration
def test_a_running_child_is_left_alone(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    record = runner.start(session, "daily")
    spawner.processes[0].exit_code = None

    runner.reconcile(session)

    assert record.status == JOB_RUNNING


@pytest.mark.integration
def test_reconciling_frees_the_guard_for_the_next_run(
    session: Session, runner: JobRunner, spawner: StubSpawner
) -> None:
    runner.start(session, "score")
    spawner.processes[0].exit_code = 0

    second = runner.start(session, "score")

    assert second.status == JOB_RUNNING


@pytest.mark.integration
def test_a_row_left_running_by_a_dead_process_is_abandoned_not_failed(
    session: Session, runner: JobRunner
) -> None:
    # What an API restart leaves behind: a RUNNING row whose child belonged to
    # the previous process. Pid 0 is never a real user process here, so it
    # stands in for one that has gone.
    orphan = JobRepository(session).start("daily", target=None, pid=0, log_path=None)

    runner.reconcile(session)

    assert orphan.status == JOB_UNKNOWN
    assert orphan.exit_code is None


@pytest.mark.integration
def test_the_log_tail_returns_what_the_job_wrote(session: Session, runner: JobRunner) -> None:
    record = runner.start(session, "score")

    assert "started" in runner.read_log(record)


@pytest.mark.integration
def test_reading_a_log_that_was_never_written_is_empty(session: Session, runner: JobRunner) -> None:
    record = JobRepository(session).start("score", target=None, pid=1, log_path=None)

    assert runner.read_log(record) == ""


MARKET_WIDE_KINDS = [name for name, spec in JOB_KINDS.items() if not spec.needs_target]


@pytest.mark.unit
@pytest.mark.parametrize("kind", MARKET_WIDE_KINDS)
def test_a_market_wide_kind_refuses_a_ticker(kind: str) -> None:
    with pytest.raises(TargetRequiredError, match="takes no ticker"):
        normalise_target(kind, "NVDA")


@pytest.mark.unit
def test_a_per_company_kind_requires_a_ticker() -> None:
    with pytest.raises(TargetRequiredError, match="needs a ticker"):
        normalise_target("research", None)


@pytest.mark.unit
@pytest.mark.parametrize("target", ["../etc/passwd", "NVDA; rm -rf /", "", "-flag", "a" * 20])
def test_a_malformed_ticker_is_rejected(target: str) -> None:
    with pytest.raises(TargetRequiredError):
        normalise_target("research", target)


@pytest.mark.unit
def test_a_ticker_is_upper_cased() -> None:
    assert normalise_target("research", "nvda") == "NVDA"
