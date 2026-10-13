"""`PAPER_DEPLOYMENT_EXECUTION_CAPABILITY_V1` — what one frozen deployment can submit.

This contract exists because P2.3.1H found an honest contradiction. The owner's
V2 deployment routes only to instruments the venue states are fractionable, so
every order it can produce is submittable. But
`BrokerCapabilities.fractional_quantities` is an **account-wide** claim, and the
Alpaca paper account contains whole-share-only securities. Deriving the
account-wide flag from the deployment's own subset would have made a true
statement about the deployment by asserting a false one about the account.

So the scope is named instead of widened. What this contract asserts is exactly:

    Every identity this frozen deployment is authorized to submit satisfies the
    required execution capability.

and never:

    The provider, or the account, supports fractional quantities everywhere.

Those are different claims. The second remains false, and the full instrument
mapping goes on recording the whole-share-only instruments that make it false —
they are simply not members of the deployment universe, so this deployment can
never produce an order for one.

Two properties make the assertion worth trusting:

- **Derived, never supplied.** The only inputs are the frozen mapping and the
  frozen universe. There is no `fractional=True` argument, no network lookup and
  no provider call, so a caller cannot assert a capability into existence.
- **Universal, not majority.** A single member that is missing from the mapping,
  not routable, not tradable, or whose fractionability the venue did not state
  makes the capability unproven. "Most of them" is how a partial claim becomes a
  rejected order.

This is an aggregate statement, and an aggregate is not a licence. The adapter
keeps its per-instrument check: see `§22` defence in depth — if a tampered
capability record claimed support for a whole-share-only instrument, the
instrument's own mapping entry still refuses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deployment import InstrumentMapping
    from .universe import DeploymentUniverse

__all__ = [
    "DEPLOYMENT_CAPABILITY_VERSION",
    "FRACTIONAL_QUANTITIES",
    "DeploymentExecutionCapability",
    "deployment_capability",
]

#: The deployment-scoped capability contract.
DEPLOYMENT_CAPABILITY_VERSION = "PAPER_DEPLOYMENT_EXECUTION_CAPABILITY_V1"

#: The capability this deployment model needs. Named so the record says which
#: capability was proven rather than carrying a bare boolean whose meaning has
#: to be inferred from the contract's name.
FRACTIONAL_QUANTITIES = "FRACTIONAL_QUANTITIES"


@dataclass(frozen=True)
class DeploymentExecutionCapability:
    """One capability proof, scoped to one frozen deployment universe.

    Attributes:
        provider: The venue.
        environment: `PAPER`.
        instrument_mapping_fingerprint: The full mapping the proof read. Full,
            not the deployment subset — the proof has to be able to find a
            member that is *absent* from routing evidence, which a mapping
            narrowed to the members could never show.
        deployment_universe_fingerprint: The universe whose members were checked.
        required_capability: Which capability was required of every member.
        eligible_identities: How many members the universe declares.
        verified_identities: How many satisfied every requirement.
        missing_identities: Members with no entry in the mapping.
        non_routable_identities: Members the mapping does not consider routable.
        non_tradable_identities: Members the venue reports as not tradable.
        non_fractionable_identities: Members the venue states need whole shares.
        unknown_fractionability_identities: Members whose fractionability the
            venue did not state. Counted apart from the previous field because
            an unstated capability is not a stated absence of one, and an
            operator reading a count of refusals should know which it was.
        supported: Whether the capability is proven **for this deployment**.
        scope: The sentence this record asserts, stored rather than left to a
            reader's memory, so a serialised proof cannot be mistaken for an
            account-wide claim once it is separated from this module.
    """

    provider: str
    environment: str
    instrument_mapping_fingerprint: str
    deployment_universe_fingerprint: str
    required_capability: str
    eligible_identities: int
    verified_identities: int
    missing_identities: tuple[str, ...] = ()
    non_routable_identities: tuple[str, ...] = ()
    non_tradable_identities: tuple[str, ...] = ()
    non_fractionable_identities: tuple[str, ...] = ()
    unknown_fractionability_identities: tuple[str, ...] = ()
    supported: bool = False
    scope: str = field(
        default=(
            "DEPLOYMENT_SCOPED — every identity this frozen deployment is authorized "
            "to submit satisfies the required execution capability. This is NOT a "
            "claim that the provider, the account or the full instrument mapping "
            "supports it: whole-share-only instruments may and do exist outside "
            "this deployment universe."
        )
    )

    @property
    def unverified(self) -> tuple[str, ...]:
        """Every member that failed, in one list, for a refusal message."""
        return (
            *self.missing_identities,
            *self.non_routable_identities,
            *self.non_tradable_identities,
            *self.non_fractionable_identities,
            *self.unknown_fractionability_identities,
        )

    def body(self) -> dict[str, Any]:
        """The proof's semantic content, for fingerprinting."""
        return {
            "version": DEPLOYMENT_CAPABILITY_VERSION,
            "provider": self.provider,
            "environment": self.environment,
            "instrument_mapping_fingerprint": self.instrument_mapping_fingerprint,
            "deployment_universe_fingerprint": self.deployment_universe_fingerprint,
            "required_capability": self.required_capability,
            "eligible_identities": self.eligible_identities,
            "verified_identities": self.verified_identities,
            "missing_identities": list(self.missing_identities),
            "non_routable_identities": list(self.non_routable_identities),
            "non_tradable_identities": list(self.non_tradable_identities),
            "non_fractionable_identities": list(self.non_fractionable_identities),
            "unknown_fractionability_identities": list(self.unknown_fractionability_identities),
            "supported": self.supported,
            "scope": self.scope,
            "account_wide_claim": False,
            "derivation": (
                "every member of the bound deployment universe looked up in the "
                "bound full mapping; universal rather than majority, and no input "
                "other than those two artefacts"
            ),
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """A digest over the proof and the two artefacts it read."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the proof as a JSON-safe mapping."""
        return {
            **self.body(),
            "capability_fingerprint": self.fingerprint(),
            "limitations": [
                "Scoped to one deployment universe and one mapping. A different "
                "universe needs its own proof, and this one stops applying the "
                "moment either fingerprint changes.",
                "An aggregate proof is not a per-order licence. The adapter keeps "
                "its per-instrument capability check, so a tampered proof cannot "
                "push a fractional quantity at a whole-share-only instrument.",
                "Venue evidence, not a timeless fact. An instrument that stops "
                "being fractionable requires a new mapping, a new universe and a "
                "reviewed binding transition.",
            ],
        }

    def require_supported(self) -> None:
        """Refuse unless the capability is proven for this deployment.

        Raises:
            RuntimeBindingError: `RUNTIME_DEPLOYMENT_CAPABILITY_UNPROVEN`, naming
                the members that failed and why. Never downgraded to a warning:
                the deployment would produce quantities the venue rejects.
        """
        from .errors import Code, RuntimeBindingError

        if self.supported:
            return
        raise RuntimeBindingError(
            Code.RUNTIME_DEPLOYMENT_CAPABILITY_UNPROVEN,
            f"{self.required_capability} is not proven for this deployment: "
            f"{self.verified_identities} of {self.eligible_identities} members "
            f"verified, and {len(self.unverified)} did not — "
            f"missing {len(self.missing_identities)}, "
            f"non-routable {len(self.non_routable_identities)}, "
            f"non-tradable {len(self.non_tradable_identities)}, "
            f"whole-share-only {len(self.non_fractionable_identities)}, "
            f"fractionability unstated {len(self.unknown_fractionability_identities)}. "
            f"First failures: {list(self.unverified[:5])}. A deployment is not "
            "authorised on a majority: one member the venue will not take a "
            "fractional quantity for is one order it will reject",
        )


def deployment_capability(
    mapping: InstrumentMapping,
    universe: DeploymentUniverse,
    *,
    required_capability: str = FRACTIONAL_QUANTITIES,
) -> DeploymentExecutionCapability:
    """Prove, or fail to prove, a capability for one frozen deployment.

    Deterministic and offline. Every member of the universe is looked up in the
    full mapping and checked against the venue's own recorded facts. Nothing is
    inferred from the strategy's intent to size by weight, and there is no
    argument by which a caller can assert the answer.

    Args:
        mapping: The **full** frozen instrument mapping. Full on purpose: a
            member absent from routing evidence is a failure this proof must be
            able to see, and a mapping narrowed to the universe's members could
            not show it.
        universe: The frozen deployment universe.
        required_capability: Which capability to require of every member. Only
            `FRACTIONAL_QUANTITIES` is modelled; another value is recorded and
            proves nothing, so an unmodelled requirement fails closed rather
            than passing by default.

    Returns:
        The proof. `supported` is True only when every member exists in the
        mapping, is routable, is tradable and is stated fractionable.
    """
    from .deployment import Routability

    by_identity = {(row.kind, row.identity_id): row for row in mapping.records}
    missing: list[str] = []
    non_routable: list[str] = []
    non_tradable: list[str] = []
    whole_share: list[str] = []
    unknown: list[str] = []
    verified = 0

    for kind, identity_id in universe.members:
        label = f"{kind}:{identity_id}"
        record = by_identity.get((kind, identity_id))
        if record is None:
            missing.append(label)
            continue
        if not record.routable:
            # `FRACTIONABILITY_UNKNOWN` is a routability outcome, but reporting
            # it as merely non-routable would lose the one fact an operator
            # needs: the instrument *is* tradable and what is missing is the
            # venue's statement about fractions. Classified as unknown so the
            # count means what §17 says it means.
            if record.routability == Routability.FRACTIONABILITY_UNKNOWN:
                unknown.append(label)
            else:
                non_routable.append(label)
            continue
        if not record.tradable:
            non_tradable.append(label)
            continue
        if required_capability != FRACTIONAL_QUANTITIES:
            # An unmodelled requirement is not satisfied by silence.
            unknown.append(label)
            continue
        if record.fractionable is None:
            unknown.append(label)
            continue
        if record.fractionable is not True:
            whole_share.append(label)
            continue
        verified += 1

    eligible = universe.size
    supported = (
        eligible > 0
        and verified == eligible
        and not (missing or non_routable or non_tradable or whole_share or unknown)
    )
    return DeploymentExecutionCapability(
        provider=mapping.provider,
        environment=mapping.environment,
        instrument_mapping_fingerprint=mapping.fingerprint(),
        deployment_universe_fingerprint=universe.fingerprint(),
        required_capability=required_capability,
        eligible_identities=eligible,
        verified_identities=verified,
        missing_identities=tuple(missing),
        non_routable_identities=tuple(non_routable),
        non_tradable_identities=tuple(non_tradable),
        non_fractionable_identities=tuple(whole_share),
        unknown_fractionability_identities=tuple(unknown),
        supported=supported,
    )
