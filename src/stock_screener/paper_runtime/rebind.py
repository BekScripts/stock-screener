"""`PAPER_OPERATIONS_RUNTIME_BINDING_V2` — superseding a binding without resetting a book.

P2.3.1E established the problem mechanically. Deploying a risk policy or an
instrument mapping needs no rebind at all — the V1 binding binds neither. The
one field that must change to enable fractional execution is
`account_binding_fingerprint`, because `ExternalBrokerAccountBinding` digests
its `capabilities` and the environment proof compares the adapter's binding
against the recorded value.

The only contract-supported way to change a bound field was to bootstrap a new
root, and `bootstrap_paper_book` opens a fresh ledger there. So the supported
path reset the accounting history to change a capability flag — which is why
P2.3.1E stopped rather than taking it.

This module adds the missing operation. Rebind is **not** bootstrap: it writes no
`OpeningBalanceEvent`, reads no cash from the broker, and preserves the
accounting generation, the execution generation, the operations lineage and the
bootstrap epoch. Those protections in `bootstrap_paper_book` and
`RuntimeBindingStore.write` are correct and are left alone; this is a different
operation beside them, not a loosening of either.

Three properties make it safe to supersede a binding at all:

- **The predecessor is immutable and still readable.** `runtime/binding.json`
  is never rewritten. A V2 binding names its predecessor's fingerprint, so the
  chain from the bootstrapped binding to the active one is reconstructible.
- **History is append-only and tamper-evident.** Each supersession is a record
  in a hash chain with a head anchor, the same shape the operations and
  execution journals use. A rebind that was rolled back locally shows as a
  chain that does not reach its anchor.
- **Only deployment metadata may change.** Account identity, strategy, the
  three generations, the bootstrap epoch, the environment and the provider are
  compared field by field against the predecessor, and a rebind that would
  alter any of them is refused. Changing those is a different operation and
  needs its own review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BINDINGS_DIRECTORY",
    "IMMUTABLE_ACROSS_REBIND",
    "REBIND_CHAIN_VERSION",
    "REBIND_GENESIS",
    "RUNTIME_BINDING_V2_VERSION",
    "PaperOperationsRuntimeBindingV2",
    "RebindRecord",
    "RuntimeBindingHistory",
    "account_capability_from_mapping",
    "active_binding",
    "rebind_runtime",
]

#: The superseding binding contract. Additive: V1 semantics are untouched and a
#: V1-only root keeps working exactly as before.
RUNTIME_BINDING_V2_VERSION = "PAPER_OPERATIONS_RUNTIME_BINDING_V2"

#: The supersession chain contract.
REBIND_CHAIN_VERSION = "PAPER_OPERATIONS_RUNTIME_REBIND_CHAIN_V1"

#: Where supersession records live, under `<root>/runtime/`.
BINDINGS_DIRECTORY = "bindings"

#: The hash a chain with no records extends from.
REBIND_GENESIS = "0" * 64

#: The fields a lineage-preserving rebind may never change. Compared field by
#: field against the predecessor, because the whole point of this operation is
#: that it touches deployment metadata and nothing else.
IMMUTABLE_ACROSS_REBIND: tuple[str, ...] = (
    "runtime_id",
    "provider",
    "environment",
    "strategy_id",
    "strategy_version",
    "strategy_fingerprint",
    "strategy_authorization_fingerprint",
    "accounting_engine_version",
    "accounting_generation",
    "execution_engine_version",
    "execution_journal_generation",
    "operations_engine_version",
    "bootstrap_epoch",
    "bootstrap_evidence_fingerprint",
)


@dataclass(frozen=True)
class PaperOperationsRuntimeBindingV2:
    """A runtime binding that supersedes another and binds its deployment inputs.

    Attributes:
        version: `PAPER_OPERATIONS_RUNTIME_BINDING_V2`.
        predecessor_fingerprint: The binding this supersedes. Never empty — a
            V2 binding exists only as a supersession, so a root with no V1
            binding has nothing to supersede.
        inherited: Every lineage and metadata field carried unchanged from the
            predecessor, by name. Includes the predecessor's
            `account_binding_fingerprint` as provenance; the active value is
            this binding's own declared field, which shadows it.
            Held as data rather than as fields so a V1 contract change does not
            silently drop a field from the superseding record.
        account_binding_fingerprint: The new venue binding digest, including its
            capabilities. The field this operation exists to change.
        risk_policy_fingerprint: The authorised paper risk policy.
        instrument_mapping_fingerprint: The verified instrument mapping.
        account_capability: The capabilities the new venue binding declares,
            and the mapping they were established from.
        reason: Why this rebind happened, for a reviewer.
        code_commit: The build that performed it.
        created_at: When.
    """

    version: str
    predecessor_fingerprint: str
    inherited: dict[str, Any]
    account_binding_fingerprint: str
    risk_policy_fingerprint: str
    instrument_mapping_fingerprint: str
    account_capability: dict[str, Any]
    reason: str
    code_commit: str
    created_at: str

    def __getattr__(self, name: str) -> Any:
        """Read an inherited V1 field as if it were declared here.

        So a caller that holds a V2 binding can ask for `accounting_generation`
        or `runtime_id` without knowing which contract version it has. Only
        inherited names resolve; anything else is an `AttributeError` as usual.
        """
        inherited = self.__dict__.get("inherited", {})
        if name in inherited:
            return inherited[name]
        raise AttributeError(name)

    def _lineage_view(self) -> Any:
        """The inherited lineage fields, read back as a V1 binding.

        Exists to answer `require_matches` and `require_strategy` rather than
        reimplementing them: a second copy of those checks would be a second
        chance to disagree with the first about which book a run may touch.

        Private, and deliberately not called `as_v1`. It is a view of the
        *lineage* only. Its `account_binding_fingerprint` is the predecessor's,
        because that field is what a rebind changes — the active value is this
        binding's own, and reading it off this view would answer the question
        the supersession exists to change.
        """
        from .binding import PaperOperationsRuntimeBinding

        return PaperOperationsRuntimeBinding.from_body(dict(self.inherited))

    def require_matches(
        self, *, accounting_generation: str, execution_journal_generation: str
    ) -> None:
        """Refuse unless the filesystem's lineages are the bound ones.

        Args:
            accounting_generation: The generation the ledger on disk reports.
            execution_journal_generation: The generation the journal reports.

        Raises:
            RuntimeBindingError: `RUNTIME_GENERATION_MISMATCH`, exactly as V1
                does — a rebind preserves the lineages, so the check it must
                pass is the unchanged one.
        """
        self._lineage_view().require_matches(
            accounting_generation=accounting_generation,
            execution_journal_generation=execution_journal_generation,
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
            RuntimeBindingError: `RUNTIME_STRATEGY_MISMATCH`. A rebind may not
                change the strategy, so this is the predecessor's own check.
        """
        self._lineage_view().require_strategy(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            fingerprint=fingerprint,
        )

    def body(self) -> dict[str, Any]:
        """The binding's semantic content, for fingerprinting."""
        return {
            "version": self.version,
            "predecessor_fingerprint": self.predecessor_fingerprint,
            "inherited": {key: self.inherited[key] for key in sorted(self.inherited)},
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "instrument_mapping_fingerprint": self.instrument_mapping_fingerprint,
            "account_capability": {
                key: self.account_capability[key] for key in sorted(self.account_capability)
            },
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """A digest over the binding."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            **self.body(),
            "reason": self.reason,
            "code_commit": self.code_commit,
            "created_at": self.created_at,
            "binding_fingerprint": self.fingerprint(),
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the binding for a terminal. Carries no credential."""
        capability = self.account_capability
        return (
            f"binding version    : {self.version}",
            f"binding fingerprint: {self.fingerprint()[:16]}…",
            f"predecessor        : {self.predecessor_fingerprint[:16]}…",
            f"account binding    : {self.account_binding_fingerprint[:16]}…",
            f"risk policy        : {self.risk_policy_fingerprint[:16]}…",
            f"instrument mapping : {self.instrument_mapping_fingerprint[:16]}…",
            f"fractional         : {capability.get('fractional_quantities')}",
            f"accounting         : {self.inherited.get('accounting_generation')}",
            f"execution journal  : {self.inherited.get('execution_journal_generation')}",
            f"bootstrap epoch    : {self.inherited.get('bootstrap_epoch')}",
        )


@dataclass(frozen=True)
class RebindRecord:
    """One supersession, chained to the one before it.

    Attributes:
        sequence: Its position, from 1.
        binding: The binding this record activates.
        previous_hash: The chain hash of the record before it.
        record_hash: This record's chain hash.
        recorded_at: When it was written.
    """

    sequence: int
    binding: PaperOperationsRuntimeBindingV2
    previous_hash: str
    record_hash: str = ""
    recorded_at: str = ""

    def body(self) -> dict[str, Any]:
        """The record's semantic content."""
        return {
            "chain_version": REBIND_CHAIN_VERSION,
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "binding": self.binding.as_json(),
        }

    def chain_hash(self) -> str:
        """This record's hash, binding it to the whole history before it."""
        from stock_screener.execution import fingerprint_of

        return hashlib.sha256(
            f"{REBIND_CHAIN_VERSION}|{self.previous_hash}|{fingerprint_of(self.body())}".encode()
        ).hexdigest()

    def as_json(self) -> dict[str, Any]:
        """Return the record as a JSON-safe mapping."""
        return {
            **self.body(),
            "record_hash": self.record_hash or self.chain_hash(),
            "recorded_at": self.recorded_at,
        }


@dataclass
class RuntimeBindingHistory:
    """The append-only supersession chain for one operations root.

    The V1 binding is the chain's origin and is never written here. Records
    supersede it in order, each naming its predecessor, and a head anchor
    records where the chain reached — so a chain truncated after the fact is
    detectable rather than merely shorter.

    Attributes:
        root: The operations root.
    """

    root: Path
    _directory: Path = field(init=False, repr=False)
    _anchor: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Resolve the chain's paths."""
        from pathlib import Path as _Path

        from .binding import RUNTIME_DIRECTORY

        self.root = _Path(self.root)
        self._directory = self.root / RUNTIME_DIRECTORY / BINDINGS_DIRECTORY
        self._anchor = self.root / RUNTIME_DIRECTORY / "bindings_head.json"

    @property
    def directory(self) -> Path:
        """Where supersession records live."""
        return self._directory

    def records(self) -> tuple[RebindRecord, ...]:
        """Every supersession, in order.

        Raises:
            RuntimeBindingError: `RUNTIME_REBIND_HISTORY_BROKEN` if a record
                cannot be read. A history that cannot be replayed establishes
                nothing about which binding is active, and guessing would mean
                operating under a binding nobody can name.
        """
        if not self._directory.exists():
            return ()
        rows: list[RebindRecord] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeBindingError(
                    Code.RUNTIME_REBIND_HISTORY_BROKEN,
                    f"{path.name} could not be read: {type(exc).__name__}",
                ) from exc
            rows.append(
                RebindRecord(
                    sequence=int(body["sequence"]),
                    binding=_binding_from_body(body["binding"]),
                    previous_hash=str(body["previous_hash"]),
                    record_hash=str(body.get("record_hash", "")),
                    recorded_at=str(body.get("recorded_at", "")),
                )
            )
        return tuple(rows)

    @property
    def head_hash(self) -> str:
        """The chain head, or genesis for an empty history."""
        rows = self.records()
        return rows[-1].record_hash if rows else REBIND_GENESIS

    def read_anchor(self) -> dict[str, Any] | None:
        """The stored head anchor, or None when there is none."""
        if not self._anchor.exists():
            return None
        try:
            body: dict[str, Any] = json.loads(self._anchor.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return body

    def verify(self) -> dict[str, Any]:
        """Check the chain and its anchor.

        Returns:
            Both claims separately: whether each record's hash follows from the
            one before it, and whether the anchor witnesses the chain head. A
            truncation shows as an anchor ahead of the chain.
        """
        rows = self.records()
        chain_ok = True
        detail = ""
        previous = REBIND_GENESIS
        for row in rows:
            expected = RebindRecord(
                sequence=row.sequence, binding=row.binding, previous_hash=previous
            ).chain_hash()
            if row.previous_hash != previous or row.record_hash != expected:
                chain_ok = False
                detail = f"record {row.sequence} does not follow the chain"
                break
            previous = row.record_hash
        anchor = self.read_anchor()
        anchored = 0 if anchor is None else int(anchor.get("sequence", 0))
        anchor_ok = anchored == len(rows) and (
            not rows or str(anchor or {}).find(rows[-1].record_hash) >= 0
        )
        return {
            "chain_version": REBIND_CHAIN_VERSION,
            "records": len(rows),
            "chain_verified": chain_ok,
            "anchor_verified": anchor_ok,
            "anchored_sequence": anchored,
            "head_hash": rows[-1].record_hash if rows else REBIND_GENESIS,
            "detail": detail,
            "not_detectable": (
                "a coordinated local rollback of both the chain and this anchor to a "
                "mutually consistent earlier state cannot be detected from local "
                "files alone"
            ),
        }

    def append(self, binding: PaperOperationsRuntimeBindingV2, *, now: str) -> RebindRecord:
        """Append one supersession, anchoring it before returning.

        The order matters and is the operations journal's: the record is written
        and read back, then the anchor is advanced. A crash between the two
        leaves the chain one ahead of its anchor, which `verify` reports rather
        than hides.

        Args:
            binding: The binding to activate.
            now: When.

        Returns:
            The appended record.
        """
        from stock_screener.paper_operations import atomic_write_verified

        rows = self.records()
        sequence = len(rows) + 1
        previous = rows[-1].record_hash if rows else REBIND_GENESIS
        draft = RebindRecord(sequence=sequence, binding=binding, previous_hash=previous)
        record = RebindRecord(
            sequence=sequence,
            binding=binding,
            previous_hash=previous,
            record_hash=draft.chain_hash(),
            recorded_at=now,
        )
        atomic_write_verified(self._directory / f"{sequence:012d}.json", record.as_json())
        atomic_write_verified(
            self._anchor,
            {
                "chain_version": REBIND_CHAIN_VERSION,
                "sequence": sequence,
                "head_hash": record.record_hash,
                "binding_fingerprint": binding.fingerprint(),
                "anchored_at": now,
            },
        )
        return record

    def active(self) -> PaperOperationsRuntimeBindingV2 | None:
        """The binding the chain currently activates, or None for a V1-only root.

        Raises:
            RuntimeBindingError: `RUNTIME_REBIND_HISTORY_BROKEN` if the chain or
                its anchor does not verify. An unverifiable history fails closed
                rather than falling back to V1: the V1 binding was superseded on
                purpose, and silently reactivating it would run under a
                capability the operator deliberately moved away from.
        """
        rows = self.records()
        if not rows:
            return None
        report = self.verify()
        if not (report["chain_verified"] and report["anchor_verified"]):
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_HISTORY_BROKEN,
                f"the rebind history does not verify (chain="
                f"{report['chain_verified']}, anchor={report['anchor_verified']}): "
                f"{report['detail']}. It does not fall back to the superseded "
                "binding — that binding was replaced deliberately",
            )
        return rows[-1].binding


def _binding_from_body(body: dict[str, Any]) -> PaperOperationsRuntimeBindingV2:
    """Rebuild a V2 binding from its stored body."""
    return PaperOperationsRuntimeBindingV2(
        version=str(body["version"]),
        predecessor_fingerprint=str(body["predecessor_fingerprint"]),
        inherited=dict(body["inherited"]),
        account_binding_fingerprint=str(body["account_binding_fingerprint"]),
        risk_policy_fingerprint=str(body["risk_policy_fingerprint"]),
        instrument_mapping_fingerprint=str(body["instrument_mapping_fingerprint"]),
        account_capability=dict(body["account_capability"]),
        reason=str(body.get("reason", "")),
        code_commit=str(body.get("code_commit", "")),
        created_at=str(body.get("created_at", "")),
    )


def active_binding(root: Path) -> Any:
    """The runtime binding in force for an operations root.

    A V2 supersession when one exists and verifies, the bootstrapped V1 binding
    otherwise. A historical V1-only root therefore keeps working unchanged — the
    contract is additive and nothing has to be rewritten to read it.

    Args:
        root: The operations root.

    Returns:
        The active binding, V1 or V2.

    Raises:
        RuntimeBindingError: `RUNTIME_NOT_BOUND` if the root holds no binding at
            all, or `RUNTIME_REBIND_HISTORY_BROKEN` if a supersession chain
            exists and cannot be verified.
    """
    from .binding import RuntimeBindingStore

    superseding = RuntimeBindingHistory(root).active()
    if superseding is not None:
        return superseding
    return RuntimeBindingStore(root).require()


def account_capability_from_mapping(mapping: Any) -> dict[str, Any]:
    """The capabilities the venue binding may declare, given a mapping.

    `BrokerCapabilities.require_quantity` is account-wide: it permits a
    fractional quantity for every instrument or for none. Fractionability, in
    contrast, is a property of the instrument. So an account-wide declaration is
    only established when *every* routable instrument in the bound mapping
    supports it — claiming it because most do would permit a fractional order
    for the ones that do not, and the venue would reject those.

    Args:
        mapping: The instrument mapping the binding will be used with.

    Returns:
        The capability declaration, naming the mapping it was derived from and
        how many instruments supported it.
    """
    routable = tuple(mapping.routable())
    fractionable = tuple(row for row in routable if row.fractionable is True)
    whole = tuple(row for row in routable if row.fractionable is False)
    account_wide = bool(routable) and not whole
    return {
        "fractional_quantities": account_wide,
        "routable_instruments": len(routable),
        "fractionable_instruments": len(fractionable),
        "whole_share_only_instruments": len(whole),
        "derived_from_mapping": mapping.fingerprint(),
        "rule": (
            "account-wide fractional capability is declared only when every "
            "routable instrument in the bound mapping is fractionable: the "
            "capability check is account-wide and fractionability is per "
            "instrument, so a partial claim would permit a fractional order for "
            "an instrument that cannot take one"
        ),
    }


def rebind_runtime(
    *,
    root: Path,
    account_binding_fingerprint: str,
    risk_policy: Any,
    mapping: Any,
    reason: str,
    now: str,
    code_commit: str = "",
    preconditions: Any = None,
    confirmed: bool = False,
) -> PaperOperationsRuntimeBindingV2:
    """Supersede a runtime binding, preserving every lineage.

    Not a bootstrap. It writes no `OpeningBalanceEvent`, reads no cash from the
    venue, and carries the accounting generation, execution generation,
    operations lineage and bootstrap epoch across unchanged. Those are compared
    field by field against the predecessor afterwards, so a rebind that would
    alter one is refused rather than recorded.

    Args:
        root: The operations root.
        account_binding_fingerprint: The new venue binding digest.
        risk_policy: The authorised paper risk policy.
        mapping: The verified instrument mapping.
        reason: Why, for a reviewer.
        now: When.
        code_commit: The build performing it.
        preconditions: A callable returning the §26 precondition report, or
            None to skip it — supplied by the caller that can actually run
            those checks, since this module reaches no venue.
        confirmed: Must be True. A rebind changes persistent operator state.

    Returns:
        The new active binding.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` without confirmation, for
            an unauthorised policy, for a live policy, or when the resulting
            binding would change a field this operation may not touch;
            `RUNTIME_REBIND_PRECONDITION_FAILED` when a precondition does not
            hold; `RUNTIME_NOT_BOUND` for a root with nothing to supersede.
    """
    from .binding import RuntimeBindingStore

    if not confirmed:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "a rebind changes persistent operator state and requires explicit "
            "confirmation. It is not implied by having the artefacts",
        )
    if not getattr(risk_policy, "authorized_for_paper", False):
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "the risk policy is not authorised for paper. A candidate is not an "
            "authority, and a rebind does not grant one",
        )
    if getattr(risk_policy, "authorized_for_live", False):
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "the risk policy claims live authorisation. Live trading is "
            "NOT_IMPLEMENTED and NOT_AUTHORIZED, and no binding here may imply it",
        )
    if str(getattr(risk_policy, "environment", "")) != "EXTERNAL_PAPER":
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            f"the risk policy names environment "
            f"{getattr(risk_policy, 'environment', '')!r}; this operation binds "
            "EXTERNAL_PAPER only",
        )

    history = RuntimeBindingHistory(root)
    predecessor = history.active() or RuntimeBindingStore(root).require()
    if preconditions is not None:
        report = preconditions()
        failures = tuple(report.get("failures", ()))
        if failures:
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_PRECONDITION_FAILED,
                f"{len(failures)} precondition(s) do not hold: {list(failures)}. A "
                "rebind in the middle of unresolved operational state would leave "
                "the execution authority ambiguous",
            )

    inherited = _inherit(predecessor)
    binding = PaperOperationsRuntimeBindingV2(
        version=RUNTIME_BINDING_V2_VERSION,
        predecessor_fingerprint=predecessor.fingerprint(),
        inherited=inherited,
        account_binding_fingerprint=account_binding_fingerprint,
        risk_policy_fingerprint=risk_policy.fingerprint(),
        instrument_mapping_fingerprint=mapping.fingerprint(),
        account_capability=account_capability_from_mapping(mapping),
        reason=reason,
        code_commit=code_commit,
        created_at=now,
    )
    _require_only_deployment_changed(predecessor, binding)
    history.append(binding, now=now)
    return binding


def _inherit(predecessor: Any) -> dict[str, Any]:
    """Carry every lineage field across from the predecessor unchanged."""
    carried = dict(getattr(predecessor, "inherited", {}) or {})
    # `version` is the *predecessor's* contract version, carried so the
    # inherited fields can be read back as the V1 binding they came from. The
    # superseding record's own version is `PAPER_OPERATIONS_RUNTIME_BINDING_V2`
    # and lives on the binding, not in here.
    carried.setdefault("version", "PAPER_OPERATIONS_RUNTIME_BINDING_V1")
    for name in (
        *IMMUTABLE_ACROSS_REBIND,
        "runtime_root",
        "account_binding_fingerprint",
        "operations_policy_fingerprint",
        "calendar_fingerprint",
        "schedule_fingerprint",
        "code_commit",
        "created_at",
    ):
        value = getattr(predecessor, name, None)
        if value is not None:
            carried[name] = value
    return carried


def _require_only_deployment_changed(predecessor: Any, binding: Any) -> None:
    """Refuse a rebind that would change anything but deployment metadata.

    Args:
        predecessor: The binding being superseded.
        binding: The superseding binding.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` naming every field that
            moved. Account identity, strategy, the three generations and the
            bootstrap epoch each need their own reviewed path, and a lineage-
            preserving rebind is not it.
    """
    moved: list[str] = []
    for name in IMMUTABLE_ACROSS_REBIND:
        before = getattr(predecessor, name, None)
        after = binding.inherited.get(name)
        if before is not None and after != before:
            moved.append(f"{name}: {before!r} -> {after!r}")
    if moved:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            f"a lineage-preserving rebind may not change {moved}. Changing an "
            "account, a strategy, a generation or the bootstrap epoch is a "
            "different operation and needs its own review",
        )
