"""What a running paper-operations instance is bound to.

Everything an operational run depends on, named once, written down, and
fingerprinted: which paper account, which strategy build, which approval, which
accounting ledger lineage, which execution journal lineage, which calendar,
which schedule, which commit.

The reason this exists rather than being inferred: the previous phases each
resolved their own state from whatever directory the process happened to start
in. That is survivable for a one-shot canary and not survivable for a system
that runs unattended, because the failure is silent. A run that opened the
canary's accounting ledger instead of the runtime's would reconcile a different
book against the same venue and report a break that was really a misconfigured
path — or worse, not report one.

So the binding is explicit and checked. A run whose filesystem disagrees with
its binding fails closed rather than proceeding against whichever book it found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PAPER_OPERATIONS_RUNTIME_BINDING_VERSION",
    "RUNTIME_DIRECTORY",
    "PaperOperationsRuntimeBinding",
    "RuntimeBindingStore",
]

#: The binding contract.
PAPER_OPERATIONS_RUNTIME_BINDING_VERSION = "PAPER_OPERATIONS_RUNTIME_BINDING_V1"

#: Where a runtime's own state lives, under the operations root. Its own
#: namespace so a runtime cannot be confused with the control plane's state or
#: with a soak's evidence.
RUNTIME_DIRECTORY = "runtime"


@dataclass(frozen=True)
class PaperOperationsRuntimeBinding:
    """One immutable description of a deployed paper-operations instance.

    Attributes:
        runtime_id: A stable name for this instance.
        runtime_root: The directory this runtime owns, as recorded.
        provider: Which broker. `ALPACA`.
        environment: Always `PAPER`. There is no live binding and no field that
            could become one.
        account_binding_fingerprint: Which paper account, as a fingerprint. The
            account number itself is never recorded.
        strategy_id: Which strategy decides.
        strategy_version: Its frozen version.
        strategy_fingerprint: Its exact policy digest.
        strategy_authorization_fingerprint: The deployment grant that approved
            that exact policy for paper.
        accounting_engine_version: Must be `PORTFOLIO_ACCOUNTING_V2`.
        accounting_generation: The ledger lineage this runtime owns.
        execution_engine_version: Must be `EXECUTION_ENGINE_V1_2`.
        execution_journal_generation: The journal lineage this runtime owns.
        operations_engine_version: The control plane version.
        operations_policy_fingerprint: Which operational rules are in force.
        calendar_fingerprint: Which sessions exist.
        schedule_fingerprint: Which slots exist.
        bootstrap_epoch: The instant this runtime's book began. Activity at the
            venue before it is historical evidence and is never runtime
            activity — see `bootstrap`.
        bootstrap_evidence_fingerprint: The digest of the account snapshot the
            book was opened from.
        code_commit: Which build was bound.
        created_at: When, ISO-8601.
    """

    runtime_id: str
    runtime_root: str
    account_binding_fingerprint: str
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    strategy_authorization_fingerprint: str
    accounting_generation: str
    execution_journal_generation: str
    operations_policy_fingerprint: str
    calendar_fingerprint: str
    schedule_fingerprint: str
    bootstrap_epoch: str
    bootstrap_evidence_fingerprint: str
    code_commit: str
    created_at: str
    provider: str = "ALPACA"
    environment: str = "PAPER"
    accounting_engine_version: str = ""
    execution_engine_version: str = ""
    operations_engine_version: str = ""
    version: str = PAPER_OPERATIONS_RUNTIME_BINDING_VERSION
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Refuse a binding that could not legitimately exist.

        Raises:
            RuntimeBindingError: `RUNTIME_BINDING_INVALID` for a missing
                identifier, generation or fingerprint, or for an engine version
                this runtime does not run under;
                `RUNTIME_LIVE_NOT_AUTHORIZED` if the environment is anything
                but `PAPER`. A wildcard account is refused for the reason P2.3
                already gives: a grant that covered any account would survive a
                repointed credential.
        """
        # The requirements are consumed from the control plane rather than
        # restated. `paper_operations.versions` already declares which execution
        # and accounting versions it will operate against, and a second copy
        # here would be a second thing to keep in agreement — with the one that
        # drifted being the one nobody was watching.
        #
        # Note `execution.EXECUTION_ENGINE_VERSION` is the *V1* constant, kept so
        # the V1 manifest keeps reporting V1. The current engine is V1.2, which
        # is what REQUIRED_EXECUTION_VERSION names.
        from stock_screener.paper_operations import (
            PAPER_OPERATIONS_ENGINE_VERSION,
            REQUIRED_ACCOUNTING_VERSION,
            REQUIRED_EXECUTION_VERSION,
        )

        object.__setattr__(
            self,
            "accounting_engine_version",
            self.accounting_engine_version or REQUIRED_ACCOUNTING_VERSION,
        )
        object.__setattr__(
            self,
            "execution_engine_version",
            self.execution_engine_version or REQUIRED_EXECUTION_VERSION,
        )
        object.__setattr__(
            self,
            "operations_engine_version",
            self.operations_engine_version or PAPER_OPERATIONS_ENGINE_VERSION,
        )
        if self.environment.upper() != "PAPER" or "LIVE" in self.environment.upper():
            raise RuntimeBindingError(
                Code.RUNTIME_LIVE_NOT_AUTHORIZED,
                f"environment={self.environment!r}. A runtime binds a PAPER venue and "
                "nothing else; live trading is NOT_IMPLEMENTED and NOT_AUTHORIZED",
            )
        required = {
            "runtime_id": self.runtime_id,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_authorization_fingerprint": self.strategy_authorization_fingerprint,
            "accounting_generation": self.accounting_generation,
            "execution_journal_generation": self.execution_journal_generation,
            "bootstrap_epoch": self.bootstrap_epoch,
        }
        missing = sorted(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                f"a runtime binding must name {missing}. Nothing here is inferred from "
                "the working directory: a run that opened another book's ledger would "
                "reconcile the wrong records against the right venue",
            )
        if self.account_binding_fingerprint.strip() in {"*", "ANY", "ALL"}:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                "a runtime may not bind a wildcard account; such a binding would "
                "survive a repointed credential",
            )
        if str(self.accounting_engine_version) != REQUIRED_ACCOUNTING_VERSION:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                f"accounting must be {REQUIRED_ACCOUNTING_VERSION}, got "
                f"{self.accounting_engine_version!r}. V2 is selected explicitly because "
                "PortfolioStore defaults to V1, and a V1 book silently replayed under "
                "V1 rules is not the book this runtime was authorised against",
            )
        if self.execution_engine_version != REQUIRED_EXECUTION_VERSION:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                f"execution must be {REQUIRED_EXECUTION_VERSION}, got "
                f"{self.execution_engine_version!r}",
            )

    def body(self) -> dict[str, Any]:
        """Return the binding's semantic content. Carries no credential."""
        return {
            "version": self.version,
            "runtime_id": self.runtime_id,
            "runtime_root": self.runtime_root,
            "provider": self.provider,
            "environment": self.environment,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_authorization_fingerprint": self.strategy_authorization_fingerprint,
            "accounting_engine_version": self.accounting_engine_version,
            "accounting_generation": self.accounting_generation,
            "execution_engine_version": self.execution_engine_version,
            "execution_journal_generation": self.execution_journal_generation,
            "operations_engine_version": self.operations_engine_version,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "bootstrap_epoch": self.bootstrap_epoch,
            "bootstrap_evidence_fingerprint": self.bootstrap_evidence_fingerprint,
            "code_commit": self.code_commit,
            "created_at": self.created_at,
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the binding."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the binding with its fingerprint."""
        return {**self.body(), "binding_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperOperationsRuntimeBinding:
        """Rebuild a binding from its stored form.

        Args:
            body: What was written.

        Returns:
            The binding.

        Raises:
            RuntimeBindingError: `RUNTIME_BINDING_UNREADABLE` if the record is
                not this contract version or a field cannot be read. Refused
                rather than defaulted: a defaulted generation would open
                whichever ledger happened to be there.
        """
        if str(body.get("version")) != PAPER_OPERATIONS_RUNTIME_BINDING_VERSION:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_UNREADABLE,
                f"binding is {body.get('version')!r}, expected "
                f"{PAPER_OPERATIONS_RUNTIME_BINDING_VERSION!r}",
            )
        try:
            return cls(
                runtime_id=str(body["runtime_id"]),
                runtime_root=str(body["runtime_root"]),
                account_binding_fingerprint=str(body["account_binding_fingerprint"]),
                strategy_id=str(body["strategy_id"]),
                strategy_version=str(body["strategy_version"]),
                strategy_fingerprint=str(body["strategy_fingerprint"]),
                strategy_authorization_fingerprint=str(body["strategy_authorization_fingerprint"]),
                accounting_generation=str(body["accounting_generation"]),
                execution_journal_generation=str(body["execution_journal_generation"]),
                operations_policy_fingerprint=str(body.get("operations_policy_fingerprint", "")),
                calendar_fingerprint=str(body.get("calendar_fingerprint", "")),
                schedule_fingerprint=str(body.get("schedule_fingerprint", "")),
                bootstrap_epoch=str(body["bootstrap_epoch"]),
                bootstrap_evidence_fingerprint=str(body.get("bootstrap_evidence_fingerprint", "")),
                code_commit=str(body.get("code_commit", "")),
                created_at=str(body.get("created_at", "")),
                provider=str(body.get("provider", "ALPACA")),
                environment=str(body.get("environment", "PAPER")),
                accounting_engine_version=str(body.get("accounting_engine_version", "")),
                execution_engine_version=str(body.get("execution_engine_version", "")),
                operations_engine_version=str(body.get("operations_engine_version", "")),
                limitations=tuple(str(row) for row in body.get("limitations", ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_UNREADABLE,
                f"binding could not be read: {type(exc).__name__}. It is refused rather "
                "than defaulted — a defaulted generation opens whichever ledger is there",
            ) from exc

    def require_matches(
        self, *, accounting_generation: str, execution_journal_generation: str
    ) -> None:
        """Refuse unless the filesystem's lineages are the bound ones.

        Args:
            accounting_generation: The generation the ledger on disk reports.
            execution_journal_generation: The generation the journal reports.

        Raises:
            RuntimeBindingError: `RUNTIME_GENERATION_MISMATCH`. This is the
                check that stops a run from operating on the canary's book, a
                test ledger, or another runtime's journal — all of which are
                real directories containing real, valid, wrong records.
        """
        problems: list[str] = []
        if accounting_generation != self.accounting_generation:
            problems.append(
                f"accounting generation on disk is {accounting_generation!r}, bound is "
                f"{self.accounting_generation!r}"
            )
        if execution_journal_generation != self.execution_journal_generation:
            problems.append(
                f"execution journal generation on disk is "
                f"{execution_journal_generation!r}, bound is "
                f"{self.execution_journal_generation!r}"
            )
        if problems:
            raise RuntimeBindingError(
                Code.RUNTIME_GENERATION_MISMATCH,
                "; ".join(problems)
                + ". The run is refused rather than proceeding against whichever book "
                "it found: those records are valid and belong to something else",
            )

    def require_strategy(
        self, *, strategy_id: str, strategy_version: str, fingerprint: str
    ) -> None:
        """Refuse unless this runtime is bound to exactly this strategy build.

        Args:
            strategy_id: The strategy asking.
            strategy_version: Its version.
            fingerprint: Its policy digest.

        Raises:
            RuntimeBindingError: `RUNTIME_STRATEGY_MISMATCH`.
        """
        if (strategy_id, strategy_version) != (self.strategy_id, self.strategy_version):
            raise RuntimeBindingError(
                Code.RUNTIME_STRATEGY_MISMATCH,
                f"this runtime is bound to {self.strategy_id}/{self.strategy_version}, "
                f"not {strategy_id}/{strategy_version}",
            )
        if fingerprint != self.strategy_fingerprint:
            raise RuntimeBindingError(
                Code.RUNTIME_STRATEGY_MISMATCH,
                f"this runtime was bound to strategy policy "
                f"{self.strategy_fingerprint[:16]}… and the strategy is "
                f"{fingerprint[:16]}…. An edited strategy is not the strategy this "
                "runtime was authorised to operate",
            )


@dataclass
class RuntimeBindingStore:
    """Where a runtime's binding lives.

    Attributes:
        root: The operations root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root and locate the runtime namespace.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if the root lies
                somewhere operational state must never be written.
        """
        from pathlib import Path as _Path

        from stock_screener.paper_operations import guard_operations_root

        self.root = _Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / RUNTIME_DIRECTORY
        self.path = self.directory / "binding.json"

    def write(self, binding: PaperOperationsRuntimeBinding) -> Path:
        """Record the binding durably, refusing to replace a different one.

        Args:
            binding: The binding.

        Returns:
            Where it was written.

        Raises:
            RuntimeBindingError: `RUNTIME_ALREADY_BOUND` if a different binding
                is already present. Re-writing the identical binding is a no-op,
                so a retried bootstrap is safe; silently repointing a live
                runtime at another account or ledger is not.
        """
        from stock_screener.paper_operations import atomic_write_verified

        existing = self.read()
        if existing is not None and existing.fingerprint() != binding.fingerprint():
            raise RuntimeBindingError(
                Code.RUNTIME_ALREADY_BOUND,
                f"this root is already bound to runtime {existing.runtime_id!r} "
                f"({existing.fingerprint()[:16]}…). A runtime is not repointed in "
                "place: bootstrap a new root instead, so the old book keeps its "
                "lineage and nothing silently changes account or ledger underneath a "
                "running soak",
            )
        atomic_write_verified(self.path, binding.as_json())
        return self.path

    def read(self) -> PaperOperationsRuntimeBinding | None:
        """Read the binding.

        Returns:
            The binding, or `None` when this root holds none.

        Raises:
            RuntimeBindingError: `RUNTIME_BINDING_UNREADABLE` if one exists and
                cannot be read.
        """
        if not self.path.exists():
            return None
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_UNREADABLE,
                f"binding at {self.path.name} could not be parsed: {type(exc).__name__}",
            ) from exc
        return PaperOperationsRuntimeBinding.from_body(body)

    def require(self) -> PaperOperationsRuntimeBinding:
        """Read the binding, refusing to run without one.

        Returns:
            The binding.

        Raises:
            RuntimeBindingError: `RUNTIME_NOT_BOUND`. The safe default is not to
                run: an unbound root has no account, no ledger lineage and no
                approved strategy, and inventing any of them is how a run acts
                on the wrong book.
        """
        binding = self.read()
        if binding is None:
            raise RuntimeBindingError(
                Code.RUNTIME_NOT_BOUND,
                f"no runtime binding at {self.path}. Run `paper-ops bootstrap-runtime "
                "--confirm` first; nothing is inferred from the working directory",
            )
        return binding
