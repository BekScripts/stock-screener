"""The authoritative operational records a soak gate may be derived from.

Every safety number a soak reports has to come from a record something durable
wrote while operating. This module is the only place that reads those records,
and it exists to make one distinction impossible to lose:

    a source that is **absent** is not a source that observed **zero**.

P2.4R3 halted because fourteen zero-tolerance gates read fields off a session
observation that never carried them, and `getattr(row, field, 0)` reported every
one of them clean. The fix is not a better default — there is no good default
for "did anything bypass the kill switch". The fix is a reader that says
`present=False` and a derivation layer that turns that into an integrity
failure instead of a zero.

Each reader returns a `SourceRead` naming the schema it read, whether the source
was there, whether its integrity held, the record identities it found and a
digest of them. A hash-chained source that does not verify is reported as
`verified=False` rather than as its contents: a journal that cannot be trusted
is evidence of nothing, and treating its records as facts would let a rewritten
chain certify itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "SOURCE_SCHEMAS",
    "SOURCE_SCHEMA_VERSION",
    "OperationalRecords",
    "SourceRead",
    "SourceSchema",
    "known_source_schema",
]

#: The source-schema registry contract.
SOURCE_SCHEMA_VERSION = "SOAK_EVIDENCE_SOURCE_SCHEMAS_V1"


@dataclass(frozen=True)
class SourceSchema:
    """One kind of authoritative record, and where it lives.

    Attributes:
        schema_id: The stable schema name. A derivation declares the schemas it
            requires by this id, and the preflight refuses an unknown one.
        version: The contract version the writer stamps.
        location: Where under the operations root it is kept, for the report.
        hash_chained: Whether integrity is established by a chain and anchor.
            A chained source that does not verify yields no records at all.
        writer: What writes it, so a reader can tell whether absence means
            "nothing happened" or "nothing writes this yet".
    """

    schema_id: str
    version: str
    location: str
    hash_chained: bool
    writer: str


#: Every source a derivation may declare. Absent from here is a preflight
#: refusal: a derivation reading a schema nobody has described is a derivation
#: nobody can audit.
SOURCE_SCHEMAS: dict[str, SourceSchema] = {
    row.schema_id: row
    for row in (
        SourceSchema(
            schema_id="OPERATIONS_RUN_JOURNAL",
            version="PAPER_OPERATIONS_JOURNAL_HASH_CHAIN_V1",
            location="run_journal/ + run_journal_anchor.json",
            hash_chained=True,
            writer="PaperOperationsCoordinator / RuntimeSubmissionAuthority",
        ),
        SourceSchema(
            schema_id="EXECUTION_JOURNAL",
            version="EXECUTION_JOURNAL_HASH_CHAIN_V1",
            location="runtime/execution/",
            hash_chained=True,
            writer="RuntimeSubmissionAuthority (P2.4I)",
        ),
        SourceSchema(
            schema_id="ACCOUNTING_LEDGER",
            version="PORTFOLIO_ACCOUNTING_V2",
            location="runtime/portfolio/",
            hash_chained=True,
            writer="PortfolioStore via admit_fill",
        ),
        SourceSchema(
            schema_id="RECONCILIATION_RECORDS",
            version="OPERATIONAL_RECONCILIATION_RECORD_V1",
            location="runtime/reconciliations/",
            hash_chained=False,
            writer="AlphaTraderPaperOperationsEnvironment.persist_reconciliation",
        ),
        SourceSchema(
            schema_id="STRATEGY_DECISIONS",
            version="PERSISTED_STRATEGY_DECISION_V1",
            location="runtime/decisions/",
            hash_chained=False,
            writer="RuntimePlanner._decide",
        ),
        SourceSchema(
            schema_id="RISK_TRANSFORMATIONS",
            version="RISK_TRANSFORMATION_V1",
            location="runtime/transformations/",
            hash_chained=False,
            writer="RuntimeSubmissionAuthority._record_transformation",
        ),
        SourceSchema(
            schema_id="SUBMISSION_INTENTS",
            version="SUBMISSION_INTENT_LOG_V1",
            location="submissions/",
            hash_chained=False,
            writer="AlpacaPaperAdapter.submit",
        ),
        SourceSchema(
            schema_id="EXTERNAL_REQUEST_AUDIT",
            version="EXTERNAL_REQUEST_AUDIT_V1",
            location="request_audit/",
            hash_chained=False,
            # Honest: the transport accepts an audit store and nothing in
            # production constructs one yet. A derivation that reads this must
            # therefore treat an empty audit beside a known mutation as missing
            # evidence, never as a clean zero.
            writer="AlpacaPaperHttpTransport when an audit store is supplied",
        ),
        SourceSchema(
            schema_id="INCIDENT_HISTORY",
            version="OPERATIONAL_INCIDENT_V1",
            location="incidents/",
            hash_chained=False,
            writer="IncidentStore via PaperOperationsCoordinator._escalate",
        ),
        SourceSchema(
            schema_id="DRILL_RECORDS",
            version="PAPER_OPERATIONS_DRILL_EVIDENCE_V2",
            location="soak/drills/",
            hash_chained=False,
            writer="DrillStoreV2",
        ),
        SourceSchema(
            schema_id="SESSION_OBSERVATIONS",
            version="PAPER_SHADOW_SESSION_OBSERVATION_V2",
            location="soak/<namespace>/observations/",
            hash_chained=False,
            writer="SoakV2Store / VersionedSoakStore",
        ),
        SourceSchema(
            schema_id="OPERATIONAL_CALENDAR",
            version="PAPER_OPERATIONS_CALENDAR_V1",
            location="declared in code, bound by protocol fingerprint",
            hash_chained=False,
            writer="operational_calendar",
        ),
    )
}


def known_source_schema(schema_id: str) -> bool:
    """Whether a derivation may declare this schema.

    Args:
        schema_id: The declared schema.

    Returns:
        True when the registry describes it.
    """
    return schema_id in SOURCE_SCHEMAS


@dataclass(frozen=True)
class SourceRead:
    """What one authoritative source yielded.

    Attributes:
        schema_id: Which source.
        present: Whether the source exists at all. `False` is the fact that
            must never become a zero.
        verified: Whether its integrity held. A chained source that fails this
            yields no records.
        records: What it held. Empty with `present=True` is an observed zero and
            is a legitimate, scoreable fact.
        identities: Stable identities of the records read, for provenance.
        fingerprint: A digest over the records read.
        detail: Why, when something is wrong.
    """

    schema_id: str
    present: bool
    verified: bool
    records: tuple[Any, ...] = ()
    identities: tuple[str, ...] = ()
    fingerprint: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether a derivation may read these records."""
        return self.present and self.verified

    def as_json(self) -> dict[str, Any]:
        """Return the read as a JSON-safe mapping, without the records."""
        return {
            "schema_id": self.schema_id,
            "schema_version": SOURCE_SCHEMAS[self.schema_id].version
            if self.schema_id in SOURCE_SCHEMAS
            else "",
            "present": self.present,
            "verified": self.verified,
            "record_count": len(self.records),
            "identities": list(self.identities),
            "fingerprint": self.fingerprint,
            "detail": self.detail,
        }


def _digest(rows: Iterable[Any]) -> str:
    """Fingerprint a sequence of records."""
    from stock_screener.execution import fingerprint_of

    return fingerprint_of({"rows": [str(row) for row in rows]})


def _read_json_dir(root: Path, *, schema_id: str) -> SourceRead:
    """Read a directory of JSON records, distinguishing absent from empty."""
    if not root.exists():
        return SourceRead(
            schema_id=schema_id,
            present=False,
            verified=False,
            detail=f"{root.name}/ does not exist; nothing has ever written this source",
        )
    paths = sorted(root.glob("*.json"))
    bodies: list[dict[str, Any]] = []
    for path in paths:
        try:
            bodies.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            return SourceRead(
                schema_id=schema_id,
                present=True,
                verified=False,
                detail=f"{path.name} could not be read: {type(exc).__name__}",
            )
    return SourceRead(
        schema_id=schema_id,
        present=True,
        verified=True,
        records=tuple(bodies),
        identities=tuple(path.stem for path in paths),
        fingerprint=_digest(sorted(json.dumps(row, sort_keys=True) for row in bodies)),
    )


@dataclass
class OperationalRecords:
    """Presence-aware readers over one operations root.

    Attributes:
        root: The operations root.
        soak_root: Where soak artefacts live, when they are not under `root`.
    """

    root: Path
    soak_root: Path | None = None
    _cache: dict[str, SourceRead] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Normalise the roots."""
        self.root = Path(self.root)
        if self.soak_root is not None:
            self.soak_root = Path(self.soak_root)

    def _cached(self, schema_id: str, read: Any) -> SourceRead:
        """Memoise one source read."""
        if schema_id not in self._cache:
            self._cache[schema_id] = read()
        return self._cache[schema_id]

    # -- hash-chained sources --------------------------------------------

    def operations_journal(self) -> SourceRead:
        """The operations run journal, verified against its head anchor."""

        def read() -> SourceRead:
            from stock_screener.paper_operations.run_journal import OperationsRunJournal

            schema = "OPERATIONS_RUN_JOURNAL"
            directory = self.root / "run_journal"
            if not directory.exists():
                return SourceRead(
                    schema_id=schema,
                    present=False,
                    verified=False,
                    detail="run_journal/ does not exist; no operational run has been recorded",
                )
            journal = OperationsRunJournal(self.root)
            try:
                report = journal.verify()
            except Exception as exc:
                # A record the journal cannot even read is the strongest form
                # of "this is not evidence". Reported rather than raised, so a
                # derivation turns it into missing evidence instead of the
                # whole score dying on an exception.
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail=(f"the operations journal does not verify: {type(exc).__name__}: {exc}"),
                )
            # Both halves, by their real names. The chain proves the records
            # were not rewritten; the anchor proves the tail was not truncated.
            # They are separate claims and a reader that checks only one — or
            # that asks for a key this report does not carry and takes the
            # default — certifies a journal it never looked at.
            missing = {"chain_verified", "anchor_verified"} - set(report)
            if missing:
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail=(
                        f"the journal integrity report does not state {sorted(missing)}; "
                        "its contract has moved and this derivation is not reading it"
                    ),
                )
            if not (report["chain_verified"] and report["anchor_verified"]):
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail=(
                        "the operations journal does not verify "
                        f"(chain={report['chain_verified']}, "
                        f"anchor={report['anchor_verified']}); its records are evidence "
                        "of nothing until a person establishes what happened"
                    ),
                )
            try:
                entries = journal.entries()
            except Exception as exc:
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail=(f"the operations journal does not verify: {type(exc).__name__}: {exc}"),
                )
            return SourceRead(
                schema_id=schema,
                present=True,
                verified=True,
                records=entries,
                identities=tuple(f"{row.sequence:012d}" for row in entries),
                fingerprint=journal.head_hash,
            )

        return self._cached("OPERATIONS_RUN_JOURNAL", read)

    def execution_journal(self) -> SourceRead:
        """The certified execution journal, verified against its head anchor."""

        def read() -> SourceRead:
            from stock_screener.execution import ExecutionStore

            schema = "EXECUTION_JOURNAL"
            directory = self.root / "runtime" / "execution"
            if not directory.exists():
                return SourceRead(
                    schema_id=schema,
                    present=False,
                    verified=False,
                    detail="runtime/execution/ does not exist",
                )
            store = ExecutionStore(directory)
            report = store.verify()
            if not bool(report.get("verified", False)):
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail="the execution journal chain does not verify",
                )
            if bool(report.get("truncation_detected", False)):
                return SourceRead(
                    schema_id=schema,
                    present=True,
                    verified=False,
                    detail="the execution journal anchor witnessed records that are gone",
                )
            records = store.load_journal().records
            return SourceRead(
                schema_id=schema,
                present=True,
                verified=True,
                records=records,
                identities=tuple(str(row.event.event_id) for row in records),
                fingerprint=str(report.get("chain", {}).get("head", "")) or _digest(records),
            )

        return self._cached("EXECUTION_JOURNAL", read)

    def accounting_ledger(self) -> SourceRead:
        """The accounting ledger's committed events."""

        def read() -> SourceRead:
            schema = "ACCOUNTING_LEDGER"
            directory = self.root / "runtime" / "portfolio" / "ledger"
            read_result = _read_json_dir(directory, schema_id=schema)
            if not read_result.usable:
                return read_result
            events = tuple(
                row.get("event", row) for row in read_result.records if isinstance(row, dict)
            )
            return SourceRead(
                schema_id=schema,
                present=True,
                verified=True,
                records=events,
                identities=tuple(str(row.get("event_id", "")) for row in events),
                fingerprint=read_result.fingerprint,
            )

        return self._cached("ACCOUNTING_LEDGER", read)

    # -- plain record directories ----------------------------------------

    def reconciliations(self) -> SourceRead:
        """Persisted reconciliation reports, by run."""
        return self._cached(
            "RECONCILIATION_RECORDS",
            lambda: _read_json_dir(
                self.root / "runtime" / "reconciliations", schema_id="RECONCILIATION_RECORDS"
            ),
        )

    def decisions(self) -> SourceRead:
        """Persisted strategy decisions, by session."""
        return self._cached(
            "STRATEGY_DECISIONS",
            lambda: _read_json_dir(
                self.root / "runtime" / "decisions", schema_id="STRATEGY_DECISIONS"
            ),
        )

    def transformations(self) -> SourceRead:
        """Recorded desired-to-approved risk transformations, by session."""
        return self._cached(
            "RISK_TRANSFORMATIONS",
            lambda: _read_json_dir(
                self.root / "runtime" / "transformations", schema_id="RISK_TRANSFORMATIONS"
            ),
        )

    def submission_intents(self) -> SourceRead:
        """Durable submission intents, by derived client order id."""
        return self._cached(
            "SUBMISSION_INTENTS",
            lambda: _read_json_dir(
                self.root / "submissions" / "submission_intents", schema_id="SUBMISSION_INTENTS"
            ),
        )

    def request_audit(self) -> SourceRead:
        """The sanitised external request audit.

        Absent is the expected answer today: the transport accepts an audit
        store and no production path supplies one. A derivation must therefore
        cross-check this against evidence that a mutation happened, and report
        missing evidence rather than a clean zero.
        """
        return self._cached(
            "EXTERNAL_REQUEST_AUDIT",
            lambda: _read_json_dir(self.root / "request_audit", schema_id="EXTERNAL_REQUEST_AUDIT"),
        )

    def incidents(self) -> SourceRead:
        """Operational incident history."""
        return self._cached(
            "INCIDENT_HISTORY",
            lambda: _read_json_dir(self.root / "incidents", schema_id="INCIDENT_HISTORY"),
        )

    def drills(self) -> SourceRead:
        """Bound drill evidence."""
        base = self.soak_root if self.soak_root is not None else self.root / "soak"
        return self._cached(
            "DRILL_RECORDS", lambda: _read_json_dir(base / "drills", schema_id="DRILL_RECORDS")
        )

    # -- convenience -----------------------------------------------------

    def journal_events(self, *, event_type: str, run_ids: tuple[str, ...] = ()) -> tuple[Any, ...]:
        """Operations-journal entries of one type, optionally for given runs.

        Args:
            event_type: The `RunEventType` name.
            run_ids: Restrict to these runs. Empty means every run.

        Returns:
            The matching entries. Empty when the journal is unusable — callers
            must check `operations_journal().usable` first rather than reading
            emptiness as absence of the event.
        """
        read = self.operations_journal()
        if not read.usable:
            return ()
        rows = [row for row in read.records if str(row.event_type) == event_type]
        if run_ids:
            allowed = set(run_ids)
            rows = [row for row in rows if str(row.run_id) in allowed]
        return tuple(rows)

    def all_reads(self) -> tuple[SourceRead, ...]:
        """Every source, read once, for a report."""
        return (
            self.operations_journal(),
            self.execution_journal(),
            self.accounting_ledger(),
            self.reconciliations(),
            self.decisions(),
            self.transformations(),
            self.submission_intents(),
            self.request_audit(),
            self.incidents(),
            self.drills(),
        )
