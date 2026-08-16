"""Running pipeline commands on behalf of the dashboard.

Every job here is an existing CLI command, spawned as a subprocess. Nothing in
this module reimplements a stage — `daily` is `run-daily`, `research` is
`research run <ticker>`, and the argument lists below are the whole mapping.

**Why a subprocess rather than calling the function.** A market-wide run takes
the better part of an hour. FastAPI serves a `def` endpoint from a threadpool,
so doing that work in-process would occupy a worker for the duration, and an
ingest that died would take the API with it. A child process keeps the API
answering, isolates a crash, and captures output to a file for free.

**Results are not returned.** Every command persists what it produces, so a
caller reads the outcome through whichever endpoint serves that thing — a
finished research job means `GET /api/stocks/{ticker}/research` now has
something. A job record answers "is it running, did it work", and nothing else.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

from data_access import JobRecord, JobRepository

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

#: How a job starts its child. See `spawn_detached` for the real one.
Spawner = Callable[[list[str], Path], "ChildProcess"]

#: Tickers are matched against this before reaching a command line. Nothing
#: user-supplied is interpolated into a shell — the child is spawned as an
#: argument list — but a job's target is also a filename component, and this
#: keeps the two concerns from ever diverging.
_TICKER = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,15}$")


class ChildProcess(Protocol):
    """The part of a spawned process this module uses.

    Narrower than `subprocess.Popen` on purpose: a test substitutes a stub
    rather than forking a real interpreter, and the two things a job needs to
    know about its child are its identity and whether it has finished.
    """

    @property
    def pid(self) -> int:
        """The process identifier."""

    def poll(self) -> int | None:
        """Return the exit code, or None while the process is still running."""


def spawn_detached(argv: list[str], log_path: Path) -> ChildProcess:
    """Start a command with its output redirected to a file.

    `start_new_session` detaches the child into its own process group, so a
    Ctrl-C in the terminal running the API does not also kill an hour-long
    ingest. It stays a child, which is what keeps its exit code recoverable.

    Args:
        argv: The command, already built from `JOB_KINDS`.
        log_path: File to write stdout and stderr to.

    Returns:
        The running process.
    """
    with log_path.open("w", encoding="utf-8") as log_file:
        # The child inherits its own descriptor, so closing this one on the way
        # out of the block does not affect it.
        return subprocess.Popen(  # noqa: S603 - argv is built from JOB_KINDS, never from input
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=Path.cwd(),
            start_new_session=True,
        )


class JobError(Exception):
    """A job could not be started."""


class UnknownJobKindError(JobError):
    """The requested kind is not one of the commands that may be run."""


class TargetRequiredError(JobError):
    """The kind needs a ticker and none was given, or vice versa."""


class JobAlreadyRunningError(JobError):
    """An identical job is already in flight.

    Attributes:
        running: The job that is already going, so a caller can report it
            rather than guessing what happened.
    """

    def __init__(self, running: JobRecord) -> None:
        super().__init__(f"{running.kind} is already running (job {running.id})")
        self.running = running


@dataclass(frozen=True)
class JobKind:
    """One command the dashboard is allowed to run.

    Attributes:
        args: The CLI arguments, appended to `python -m stock_screener`.
        label: Human name, shown in the interface.
        needs_target: Whether a ticker must be supplied.
        spends_money: Whether running it can incur a provider charge. Drives the
            confirmation the interface asks for, and nothing else.
        minutes: Rough duration, so the interface can set an expectation.
    """

    args: tuple[str, ...]
    label: str
    needs_target: bool = False
    spends_money: bool = False
    minutes: int = 1


#: Every command reachable from the dashboard. A kind absent from here cannot be
#: run, which is what keeps the endpoint from being a way to execute arbitrary
#: commands: the caller picks a key, never an argument.
JOB_KINDS: dict[str, JobKind] = {
    "daily": JobKind(("run-daily",), "Daily pipeline", minutes=60),
    "scan": JobKind(("scan",), "Eligibility scan", minutes=1),
    "score": JobKind(("score",), "Score the market", minutes=1),
    "enrich": JobKind(("enrich",), "Enrich top candidates", spends_money=True, minutes=5),
    "research": JobKind(
        ("research", "run"), "AI research", needs_target=True, spends_money=True, minutes=2
    ),
    "update-universe": JobKind(("update-universe",), "Refresh the universe", minutes=1),
    "update-market": JobKind(("update-market",), "Refresh prices", minutes=10),
    "update-benchmark": JobKind(("update-benchmark",), "Refresh the benchmark", minutes=1),
    "update-fundamentals": JobKind(("update-fundamentals",), "Refresh fundamentals", minutes=45),
}


def normalise_target(kind: str, target: str | None) -> str | None:
    """Validate a job's target against what its kind expects.

    Args:
        kind: The job kind, already known to exist.
        target: The ticker supplied by the caller, if any.

    Returns:
        The upper-cased ticker, or None for a kind that takes no target.

    Raises:
        TargetRequiredError: If a ticker is missing, malformed, or supplied to a kind
            that runs over the whole market.
    """
    spec = JOB_KINDS[kind]

    if not spec.needs_target:
        if target:
            raise TargetRequiredError(f"{kind} runs over every company and takes no ticker")
        return None

    if not target:
        raise TargetRequiredError(f"{kind} needs a ticker")
    if not _TICKER.match(target):
        raise TargetRequiredError(f"not a ticker: {target!r}")

    return target.upper()


class JobRunner:
    """Starts pipeline commands and keeps their records honest.

    One instance per API process. It owns no state of its own — everything it
    knows is a row — so a second instance would behave identically.
    """

    def __init__(self, settings: Settings, *, spawn: Spawner = spawn_detached) -> None:
        """Build a runner.

        Args:
            settings: Supplies the log directory.
            spawn: How to start a child. Injected so a test can substitute a
                stub instead of forking an interpreter per case.
        """
        self._settings = settings
        self._spawn = spawn
        #: The children this process spawned, by job id. Holding the handle is
        #: what makes an exit code recoverable: `Popen.poll` reaps the child and
        #: caches its status, whereas a bare `waitpid` races with `subprocess`'s
        #: own cleanup and a bare `kill(pid, 0)` cannot see that a zombie has
        #: finished. A job missing from here was started by a previous API
        #: process and can only be judged by whether its pid still exists.
        self._children: dict[int, ChildProcess] = {}

    def start(self, session: Session, kind: str, *, target: str | None = None) -> JobRecord:
        """Spawn a command and record it.

        Args:
            session: Open database session. The caller commits.
            kind: A key of `JOB_KINDS`.
            target: The ticker, for a per-company kind.

        Returns:
            The stored record of the job now running.

        Raises:
            UnknownJobKindError: If the kind is not one that may be run.
            TargetRequiredError: If the ticker is missing, malformed, or unwanted.
            JobAlreadyRunningError: If an identical job is already in flight. Two
                concurrent market-wide runs would race on the same rows and
                double the load on the providers they read.
        """
        if kind not in JOB_KINDS:
            raise UnknownJobKindError(f"unknown job: {kind}")

        ticker = normalise_target(kind, target)
        repository = JobRepository(session)

        self.reconcile(session)

        existing = repository.running(kind, target=ticker)
        if existing is not None:
            raise JobAlreadyRunningError(existing)

        record = repository.start(kind, target=ticker, pid=None, log_path=None)
        log_path = self._log_path(record.id, kind, ticker)
        argv = self._argv(kind, ticker)

        try:
            process = self._spawn(argv, log_path)
        except OSError:
            repository.finish(record.id, exit_code=-1)
            log.exception("job failed to start", job_id=record.id, kind=kind)
            raise

        self._children[record.id] = process
        record.pid = process.pid
        record.log_path = str(log_path)
        session.flush()

        log.info(
            "job started",
            job_id=record.id,
            kind=kind,
            target=ticker,
            pid=process.pid,
            command=shlex.join(argv),
        )
        return record

    def reconcile(self, session: Session) -> list[JobRecord]:
        """Close out any job whose process is no longer alive.

        A job row is written by one process and the work happens in another, so
        nothing updates the row when the child exits. This is what does it,
        called before any read the interface depends on.

        Args:
            session: Open database session. The caller commits.

        Returns:
            The records whose status changed.
        """
        repository = JobRepository(session)
        changed: list[JobRecord] = []

        for record in repository.all_running():
            child = self._children.get(record.id)

            if child is not None:
                exit_code = child.poll()
                if exit_code is None:
                    continue
                del self._children[record.id]
                updated = repository.finish(record.id, exit_code=exit_code)
            else:
                # Started by an earlier API process. Its exit status went with
                # that process, so the most that can be said is whether the pid
                # is still there — and a run that may well have completed is not
                # something to record as a failure.
                if record.pid is None or not _is_alive(record.pid):
                    updated = repository.abandon(record.id)
                else:
                    continue

            if updated is not None:
                changed.append(updated)
                log.info("job reconciled", job_id=record.id, status=updated.status)

        return changed

    def read_log(self, record: JobRecord, *, tail: int = 200) -> str:
        """Return the last lines a job wrote.

        Args:
            record: The job whose output to read.
            tail: How many lines from the end. The interface polls this, so it
                is a tail rather than the whole file.

        Returns:
            The lines, joined. Empty when the job has written nothing yet or
            its log has been removed.
        """
        if record.log_path is None:
            return ""

        path = Path(record.log_path)
        if not path.is_file():
            return ""

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def _argv(self, kind: str, target: str | None) -> list[str]:
        """Build the command line for a kind.

        `sys.executable -m` rather than the console script: it runs whatever
        interpreter the API is running under, with no dependence on `uv` or the
        console script being on PATH.
        """
        argv = [sys.executable, "-m", "stock_screener", *JOB_KINDS[kind].args]
        if target is not None:
            argv.append(target)
        return argv

    def _log_path(self, job_id: int, kind: str, target: str | None) -> Path:
        """Return the file a job's output goes to, creating its directory."""
        directory = Path(self._settings.job_log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"-{target}" if target else ""
        return directory / f"{job_id:06d}-{kind}{suffix}.log"


def _is_alive(pid: int) -> bool:
    """Return whether a process exists.

    Signal 0 performs the permission and existence checks without delivering
    anything, which is the cheapest way to ask.

    Non-positive pids are rejected before they get near `kill`, where 0 means
    "every process in my group" and -1 means "every process I may signal".
    Both would report success and leave the job running forever.
    """
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just is not ours. Treat that as alive rather than
        # silently closing a job that may still be writing.
        return True
    return True
