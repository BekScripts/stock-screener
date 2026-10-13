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
    "RUNTIME_BINDING_V3_VERSION",
    "STRATEGY_REVISION_FIELDS",
    "STRATEGY_REVISION_VERSION",
    "V3_DEPLOYMENT_FIELDS",
    "PaperOperationsRuntimeBindingV2",
    "PaperOperationsRuntimeBindingV3",
    "RebindRecord",
    "RuntimeBindingHistory",
    "account_capability_from_mapping",
    "active_binding",
    "rebind_runtime",
    "revise_deployment",
    "revise_strategy",
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


#: The corrected deployment-binding contract. Additive: V1 and V2 keep their
#: exact semantics and their fingerprints, and V2 stays in the codebase as the
#: reviewed-but-insufficient contract P2.3.1H measured. What V2 could not do is
#: bind a deployment universe or a deployment-scoped capability, so a runtime on
#: V2 could claim to deploy V2's strategy while having no way to say which
#: identities it was allowed to reach.
RUNTIME_BINDING_V3_VERSION = "PAPER_OPERATIONS_RUNTIME_BINDING_V3"

#: The fields V3 declares beyond V2, each required and each with a production
#: consumer. Data so the coverage guard can check them rather than trusting a
#: docstring that says they are wired.
V3_DEPLOYMENT_FIELDS: tuple[str, ...] = (
    "risk_engine_version",
    "deployment_universe_version",
    "deployment_universe_fingerprint",
    "deployment_capability_fingerprint",
)

#: The strategy-revision transition contract. A *distinct* operation from a
#: lineage-preserving rebind, and named so an auditor reading the chain can see
#: which of the two happened without inferring it from which fields moved.
STRATEGY_REVISION_VERSION = "PAPER_OPERATIONS_STRATEGY_REVISION_V1"

#: The only fields a strategy revision may move, and the reason this is a
#: separate tuple rather than a deletion from `IMMUTABLE_ACROSS_REBIND`: the
#: invariant is not weakened, it is made conditional on an operation that says
#: out loud what it is changing. A plain rebind still passes no exemptions and
#: still refuses every one of these. `strategy_id` is deliberately absent — a
#: different strategy is a different runtime, not a revision of this one — and
#: so are all three generations and the bootstrap epoch, because a revised
#: selection rule does not restart the books.
STRATEGY_REVISION_FIELDS: tuple[str, ...] = (
    "strategy_version",
    "strategy_fingerprint",
    "strategy_authorization_fingerprint",
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
        transition: Which transition produced this binding. Defaults to the
            lineage-preserving rebind and is omitted from the fingerprinted
            body when it is that default, so every rebind recorded before this
            field existed digests to exactly what it digested to. A strategy
            revision sets it, and then the chain states which operation
            happened instead of leaving it to be inferred from which fields
            moved.
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
    transition: str = REBIND_CHAIN_VERSION

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
        body: dict[str, Any] = {
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
        if self.transition != REBIND_CHAIN_VERSION:
            body["transition"] = self.transition
        return body

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
class PaperOperationsRuntimeBindingV3(PaperOperationsRuntimeBindingV2):
    """A runtime binding that also binds what the deployment may reach.

    Subclasses V2 so every lineage, supersession and chain semantic is literally
    the same code rather than a second implementation that could disagree. The
    four added fields are the ones P2.3.1H proved missing: without them a
    binding can name a strategy that only deploys across a frozen universe and
    then fail to say which universe, which is a deployment authority with its
    subject left blank.

    Attributes:
        risk_engine_version: Which engine the bound policy is authorised
            against. Recorded rather than inferred from the policy, because the
            engine a decision ran under is what makes it reproducible.
        deployment_universe_version: The universe contract.
        deployment_universe_fingerprint: The exact universe. Production loads
            the artefact and refuses unless it digests to this.
        deployment_capability_fingerprint: The exact deployment-scoped
            capability proof. Scoped, never an account-wide claim — see
            `deployment_capability`.
    """

    risk_engine_version: str = ""
    deployment_universe_version: str = ""
    deployment_universe_fingerprint: str = ""
    deployment_capability_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Refuse a V3 binding with a blank deployment field.

        The fields carry `""` defaults for dataclass inheritance only. A blank
        one is refused here, so the contract cannot be satisfied by omission —
        which is how the V2 gap would otherwise reappear as a V3 binding that
        names no universe.

        Raises:
            RuntimeBindingError: `RUNTIME_REBIND_REFUSED` naming every blank
                field.
        """
        blank = [name for name in V3_DEPLOYMENT_FIELDS if not str(getattr(self, name, "")).strip()]
        if blank:
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_REFUSED,
                f"a {RUNTIME_BINDING_V3_VERSION} binding must state {blank}. A "
                "deployment binding with a blank universe or capability is the "
                "defect this contract exists to close, and an empty string is "
                "not a value",
            )

    def body(self) -> dict[str, Any]:
        """The binding's semantic content, V2's plus the deployment fields."""
        return {
            **super().body(),
            "risk_engine_version": self.risk_engine_version,
            "deployment_universe_version": self.deployment_universe_version,
            "deployment_universe_fingerprint": self.deployment_universe_fingerprint,
            "deployment_capability_fingerprint": self.deployment_capability_fingerprint,
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the binding for a terminal, with its deployment authority."""
        return (
            *super().summary_lines(),
            f"risk engine               : {self.risk_engine_version}",
            f"deployment universe       : {self.deployment_universe_version}",
            f"universe fingerprint      : {self.deployment_universe_fingerprint[:32]}…",
            f"capability fingerprint    : {self.deployment_capability_fingerprint[:32]}…",
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
    """Rebuild a superseding binding from its stored body.

    Dispatches on the recorded version. A V3 record rebuilds as V3 with its
    deployment fields read back by name — reading it as a V2 would drop them
    silently and the chain hash would stop reproducing, which is the write-path
    / read-path defect this project has now hit nine times.
    """
    carried: dict[str, Any] = {}
    if str(body.get("version", "")) == RUNTIME_BINDING_V3_VERSION:
        carried = {name: str(body[name]) for name in V3_DEPLOYMENT_FIELDS if name in body}
        return PaperOperationsRuntimeBindingV3(
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
            transition=str(body.get("transition", REBIND_CHAIN_VERSION)),
            **carried,
        )
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
        # Read back by name, defaulting to the rebind transition: a record
        # written before this field existed carries no key, and a record that
        # does carry one must round-trip to the same body or its chain hash
        # will not reproduce.
        transition=str(body.get("transition", REBIND_CHAIN_VERSION)),
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
    _require_paper_policy(risk_policy)

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


def _require_only_deployment_changed(
    predecessor: Any, binding: Any, *, allowed: tuple[str, ...] = ()
) -> None:
    """Refuse a rebind that would change anything but deployment metadata.

    Args:
        predecessor: The binding being superseded.
        binding: The superseding binding.
        allowed: Immutable fields this particular transition is permitted to
            move. Empty by default, which is the lineage-preserving rebind and
            is unchanged: it exempts nothing. A caller must name an exemption
            explicitly, and only `revise_strategy` does, so the invariant stays
            exactly as strong for every operation that does not declare itself
            a strategy revision.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` naming every field that
            moved. Account identity, strategy, the three generations and the
            bootstrap epoch each need their own reviewed path, and a lineage-
            preserving rebind is not it.
    """
    moved: list[str] = []
    for name in IMMUTABLE_ACROSS_REBIND:
        if name in allowed:
            continue
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


def revise_strategy(
    *,
    root: Path,
    strategy: Any,
    authorization: Any,
    risk_policy: Any,
    mapping: Any,
    account_binding_fingerprint: str,
    reason: str,
    now: str,
    code_commit: str = "",
    decisions: Any = None,
    confirmed: bool = False,
) -> PaperOperationsRuntimeBindingV2:
    """Bind a runtime to a revised strategy, preserving every other lineage.

    A distinct operation from `rebind_runtime`, and distinct for a reason worth
    stating. A rebind says *the same strategy now routes through different
    deployment inputs*; this says *a different, separately authorised strategy
    now decides for the same books*. Both preserve the accounting generation,
    the execution journal generation, the operations lineage and the bootstrap
    epoch — neither is a bootstrap and neither writes an opening balance — but
    only this one may move the strategy's version, fingerprint and grant, and
    it says so in the chain rather than leaving an auditor to infer it.

    The strategy-fingerprint invariant is not weakened to allow this. It is
    passed an explicit exemption naming the three fields this transition exists
    to change; every other caller passes none, so a rebind that quietly carried
    a new strategy is refused exactly as before.

    Args:
        root: The operations root.
        strategy: The revised strategy. Its `strategy_id`, `strategy_version`
            and `fingerprint()` are read; nothing is decided here.
        authorization: The deployment grant for *this* strategy. Verified
            against the strategy's own fingerprint, so a grant approving the
            predecessor cannot carry the revision.
        risk_policy: The authorised paper risk policy.
        mapping: The verified instrument mapping.
        account_binding_fingerprint: The venue binding digest.
        reason: Why, for a reviewer.
        now: When.
        code_commit: The build performing it.
        decisions: A `StrategyDecisionStore`, or None to skip the check. When
            supplied, a decision still awaiting execution refuses the revision.
        confirmed: Must be True.

    Returns:
        The new active binding.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` without confirmation, for
            an unauthorised or live-claiming policy, for a grant that does not
            cover this exact strategy, for a revision that would change the
            strategy *identity* rather than its version, or when any field
            outside the declared exemption moved;
            `RUNTIME_REBIND_PRECONDITION_FAILED` when a decision is pending;
            `RUNTIME_NOT_BOUND` for a root with nothing to supersede.
    """
    from .binding import RuntimeBindingStore

    if not confirmed:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "a strategy revision changes which policy decides for a live set of "
            "books and requires explicit confirmation. It is not implied by "
            "having a newer strategy available",
        )
    _require_paper_policy(risk_policy)

    fingerprint = strategy.fingerprint()
    strategy_version = str(strategy.strategy_version)
    strategy_id = str(strategy.strategy_id)
    # The grant is checked against the strategy's own digest before anything is
    # written. A grant names one exact policy, so this is what stops the
    # predecessor's approval from being stretched over a revision nobody
    # reviewed.
    authorization.require(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        fingerprint=fingerprint,
        environment="EXTERNAL_PAPER",
    )

    history = RuntimeBindingHistory(root)
    predecessor = history.active() or RuntimeBindingStore(root).require()
    if str(getattr(predecessor, "strategy_id", strategy_id)) != strategy_id:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            f"this runtime is bound to strategy "
            f"{getattr(predecessor, 'strategy_id', '')!r} and the revision names "
            f"{strategy_id!r}. A revision changes a strategy's version, never its "
            "identity: a different strategy deciding for these books is a new "
            "runtime, because its decisions would share an accounting lineage "
            "with decisions it never made",
        )
    if fingerprint == getattr(predecessor, "strategy_fingerprint", None):
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "the revised strategy fingerprints identically to the bound one, so "
            "there is nothing to revise. Recording a transition that changed "
            "nothing would put a reviewed-looking entry in the chain for a "
            "review that had no subject",
        )

    if decisions is not None:
        pending = decisions.pending()
        if pending is not None:
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_PRECONDITION_FAILED,
                f"decision {pending.decision.decision_session} is "
                f"{pending.status} and has not reached a terminal state. Revising "
                "the strategy underneath it would leave a decision in the store "
                "that the bound strategy did not make and cannot be re-derived "
                "from — it is neither executable nor explainable. Let it execute, "
                "miss or be superseded first",
            )

    inherited = _inherit(predecessor)
    inherited.update(
        {
            "strategy_version": strategy_version,
            "strategy_fingerprint": fingerprint,
            "strategy_authorization_fingerprint": authorization.fingerprint(),
        }
    )
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
        transition=STRATEGY_REVISION_VERSION,
    )
    _require_only_deployment_changed(predecessor, binding, allowed=STRATEGY_REVISION_FIELDS)
    history.append(binding, now=now)
    return binding


def _require_paper_policy(risk_policy: Any) -> None:
    """Refuse a risk policy that does not authorise exactly paper.

    One definition, shared by both transitions, so a second transition cannot
    be added with a quietly laxer version of the same three checks.

    Args:
        risk_policy: The policy offered.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` for a policy that is not
            paper-authorised, that claims live authorisation, or that names an
            environment other than `EXTERNAL_PAPER`.
    """
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


def revise_deployment(
    *,
    root: Path,
    strategy: Any,
    authorization: Any,
    risk_policy: Any,
    mapping: Any,
    universe: Any,
    capability: Any,
    account_binding_fingerprint: str,
    reason: str,
    now: str,
    code_commit: str = "",
    decisions: Any = None,
    executions: Any = None,
    confirmed: bool = False,
) -> PaperOperationsRuntimeBindingV3:
    """Supersede a runtime binding with a corrected V3 deployment authority.

    The transition P2.3.1H could not perform. It revises the strategy, its
    grant, the risk policy, the instrument-mapping binding, the deployment
    universe and the deployment-scoped capability in one reviewed step, and it
    binds all of them — which is precisely what V2 could not do.

    It supersedes whatever is active, V1 included, and does **not** require a V2
    binding in the chain first. V2 was reviewed and found insufficient; putting
    one in the lineage to reach V3 would record a deployment that was never
    operated. The reason this transition writes says so.

    Not a bootstrap. No `OpeningBalanceEvent`, no cash read, and the accounting
    generation, execution generation, operations lineage and bootstrap epoch all
    carry across and are compared field by field afterwards.

    Args:
        root: The operations root.
        strategy: The revised strategy. Read for its id, version and
            fingerprint; nothing is decided here.
        authorization: The deployment grant for this exact strategy.
        risk_policy: The authorised paper risk policy.
        mapping: The **full** frozen instrument mapping. Full: it stays the
            routing evidence, including the instruments the deployment excludes.
        universe: The frozen deployment universe.
        capability: The deployment-scoped capability proof. Must be proven, and
            must have been derived from exactly this mapping and universe.
        account_binding_fingerprint: The venue binding digest. Account identity,
            and never widened to carry the deployment's capability.
        reason: Why, for a reviewer.
        now: When.
        code_commit: The build performing it.
        decisions: A `StrategyDecisionStore`, or None to skip. A decision
            awaiting execution refuses the transition.
        executions: Anything exposing `remote_state_unknown()` or
            `partially_executed()`, or None to skip.
        confirmed: Must be True.

    Returns:
        The new active binding.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_REFUSED` without confirmation, for
            an unauthorised or live-claiming policy, for a grant that does not
            cover this strategy, for a changed strategy identity, or when a
            field outside the declared exemption moved;
            `RUNTIME_DEPLOYMENT_CAPABILITY_UNPROVEN` for an unproven capability;
            `RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH` when the capability
            was not derived from the mapping and universe being bound;
            `RUNTIME_REBIND_PRECONDITION_FAILED` for in-flight authority;
            `RUNTIME_NOT_BOUND` for a root with nothing to supersede.
    """
    from .binding import RuntimeBindingStore
    from .deployment_capability import DEPLOYMENT_CAPABILITY_VERSION
    from .universe import DEPLOYMENT_UNIVERSE_VERSION

    if not confirmed:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            "a deployment revision changes which policy decides for a live set "
            "of books and which identities it may reach, and requires explicit "
            "confirmation. It is not implied by having the artefacts",
        )
    _require_paper_policy(risk_policy)

    # The universe must come from the mapping being bound, and the capability
    # from both. Checked before anything is written: a proof about other
    # evidence is not a proof about this deployment.
    if universe.mapping_fingerprint != mapping.fingerprint():
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH,
            f"the deployment universe was derived from mapping "
            f"{universe.mapping_fingerprint[:16]}… and the mapping being bound is "
            f"{mapping.fingerprint()[:16]}…. A universe and the routing evidence "
            "it was derived from are one artefact pair, and crossing them would "
            "authorise identities against evidence nobody checked",
        )
    if (
        capability.instrument_mapping_fingerprint != mapping.fingerprint()
        or capability.deployment_universe_fingerprint != universe.fingerprint()
    ):
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_FINGERPRINT_MISMATCH,
            "the capability proof was derived from a different mapping or "
            "universe than the ones being bound. A capability is a statement "
            "about one exact deployment and does not transfer to another",
        )
    capability.require_supported()

    fingerprint = strategy.fingerprint()
    strategy_version = str(strategy.strategy_version)
    strategy_id = str(strategy.strategy_id)
    authorization.require(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        fingerprint=fingerprint,
        environment="EXTERNAL_PAPER",
    )

    history = RuntimeBindingHistory(root)
    predecessor = history.active() or RuntimeBindingStore(root).require()
    if str(getattr(predecessor, "strategy_id", strategy_id)) != strategy_id:
        raise RuntimeBindingError(
            Code.RUNTIME_REBIND_REFUSED,
            f"this runtime is bound to strategy "
            f"{getattr(predecessor, 'strategy_id', '')!r} and the revision names "
            f"{strategy_id!r}. A revision changes a strategy's version, never its "
            "identity: a different strategy deciding for these books would share "
            "an accounting lineage with decisions it never made",
        )
    _require_no_inflight_authority(decisions, executions)

    inherited = _inherit(predecessor)
    inherited.update(
        {
            "strategy_version": strategy_version,
            "strategy_fingerprint": fingerprint,
            "strategy_authorization_fingerprint": authorization.fingerprint(),
        }
    )
    binding = PaperOperationsRuntimeBindingV3(
        version=RUNTIME_BINDING_V3_VERSION,
        predecessor_fingerprint=predecessor.fingerprint(),
        inherited=inherited,
        account_binding_fingerprint=account_binding_fingerprint,
        risk_policy_fingerprint=risk_policy.fingerprint(),
        instrument_mapping_fingerprint=mapping.fingerprint(),
        # Account identity's own capability record, unchanged in meaning: it
        # still describes the full mapping and still says fractional support is
        # not account-wide. The deployment's capability is a separate field.
        account_capability=account_capability_from_mapping(mapping),
        reason=reason,
        code_commit=code_commit,
        created_at=now,
        transition=STRATEGY_REVISION_VERSION,
        risk_engine_version=str(getattr(risk_policy, "engine_version", "")),
        deployment_universe_version=DEPLOYMENT_UNIVERSE_VERSION,
        deployment_universe_fingerprint=universe.fingerprint(),
        deployment_capability_fingerprint=capability.fingerprint(),
    )
    assert binding.account_capability["derived_from_mapping"] == mapping.fingerprint()  # noqa: S101
    assert DEPLOYMENT_CAPABILITY_VERSION in capability.body()["version"]  # noqa: S101
    _require_only_deployment_changed(predecessor, binding, allowed=STRATEGY_REVISION_FIELDS)
    history.append(binding, now=now)
    return binding


def _require_no_inflight_authority(decisions: Any, executions: Any) -> None:
    """Refuse a transition while any execution authority is unresolved.

    Args:
        decisions: A decision store, or None.
        executions: An execution-state reporter, or None.

    Raises:
        RuntimeBindingError: `RUNTIME_REBIND_PRECONDITION_FAILED` for a pending
            decision, a partially executed plan, or unknown remote state. None
            of the three is migrated: a decision the newly bound strategy did
            not make is neither executable under it nor explainable by it.
    """
    if decisions is not None:
        pending = decisions.pending()
        if pending is not None:
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_PRECONDITION_FAILED,
                f"decision {pending.decision.decision_session} is "
                f"{pending.status} and has not reached a terminal state. "
                "Revising the deployment underneath it would leave a decision "
                "the bound strategy did not make and cannot be re-derived from. "
                "Let it execute, miss or be superseded first",
            )
    if executions is None:
        return
    for probe, label in (
        ("remote_state_unknown", "remote state is unknown"),
        ("partially_executed", "a plan is partially executed"),
    ):
        check = getattr(executions, probe, None)
        if callable(check) and check():
            raise RuntimeBindingError(
                Code.RUNTIME_REBIND_PRECONDITION_FAILED,
                f"{label}, so this runtime's execution authority is unresolved. "
                "A deployment revision over an ambiguous execution state would "
                "make it impossible to say afterwards which authority a fill "
                "belonged to",
            )
