"""`ALPHATRADER_PAPER_FRACTIONABLE_UNIVERSE_V1` — where the paper strategy may deploy.

A deployment universe is not a research universe, and keeping them apart is the
whole point of this module. Compounder eligibility answers *is this a company
worth holding*; this answers *can an order for it actually be placed in the
paper account as the strategy intends*. A company can pass the first and fail
the second, and when it does the record says so in those words.

The restriction exists because the strategy expresses exposure as a weight and
the risk engine turns a weight into a quantity that is almost never a whole
number. An instrument the venue will only trade in whole shares therefore
cannot receive the exposure that was authorised — it would be rounded, and a
rounded position is a different position from the one the engine approved.

Two properties are load-bearing:

- **Derived, never authored.** Every entry comes from the instrument mapping,
  which is provider evidence. There is no hand-maintained include list, no
  symbol allowlist and no override, because a universe somebody can edit is a
  universe that stops describing the venue.
- **Identity, never symbol.** Membership is keyed on the durable identity. A
  symbol is reassigned when a company delists and another takes the ticker, and
  a universe keyed on symbols would silently admit the successor.

What this module never does: decide what is worth owning, read a price, read a
score, or widen a universe to reach a target count. It narrows, and it records
the reason for every identity it removes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deployment import InstrumentMapping, InstrumentRecord

__all__ = [
    "DEPLOYMENT_UNIVERSE_VERSION",
    "DeploymentExclusion",
    "DeploymentUniverse",
    "UniverseExclusion",
    "fractionable_universe",
]

#: The deployment-universe contract.
DEPLOYMENT_UNIVERSE_VERSION = "ALPHATRADER_PAPER_FRACTIONABLE_UNIVERSE_V1"


class UniverseExclusion:
    """Why an identity is not in the deployment universe.

    The routability reasons are reused verbatim from `Routability` rather than
    flattened into one "not deployable": an identity the venue never heard of
    and an identity the venue trades only in whole shares are different facts
    about the venue, and an operator reading a count of the second should not
    have to wonder whether it is really the first.
    """

    WHOLE_SHARE_ONLY = "WHOLE_SHARE_ONLY"
    """Routable, and the venue states it is not fractionable. The one exclusion
    this contract adds to routability: the instrument is perfectly tradable and
    what it cannot take is the fractional quantity the strategy's weights
    produce."""

    @staticmethod
    def for_record(record: InstrumentRecord) -> str | None:
        """Why this record is excluded, or `None` when it is deployable.

        Args:
            record: The mapped instrument.

        Returns:
            The exclusion reason. A non-routable record carries its own
            routability reason — including `FRACTIONABILITY_UNKNOWN`, which is
            not reported as `WHOLE_SHARE_ONLY` because an unstated capability
            is not a stated absence of one. A routable record that the venue
            says is not fractionable carries `WHOLE_SHARE_ONLY`.
        """
        if not record.routable:
            return record.routability
        if record.fractionable is not True:
            return UniverseExclusion.WHOLE_SHARE_ONLY
        return None


@dataclass(frozen=True)
class DeploymentExclusion:
    """One identity the deployment universe does not contain.

    Attributes:
        kind: The identity kind.
        identity_id: The durable internal id.
        reason: A `UniverseExclusion` or `Routability` value.
    """

    kind: str
    identity_id: int
    reason: str

    @property
    def label(self) -> str:
        """The identity label the rest of the system uses."""
        return f"{self.kind}:{self.identity_id}"

    def body(self) -> dict[str, Any]:
        """The exclusion's semantic content."""
        return {"kind": self.kind, "identity_id": self.identity_id, "reason": self.reason}


@dataclass(frozen=True)
class DeploymentUniverse:
    """The identities the paper strategy may deploy across.

    Attributes:
        members: Every deployable identity, as `(kind, identity_id)` pairs in
            ascending order. Pairs rather than labels so nothing has to parse a
            string back into an identity, and no symbol appears at all.
        exclusions: Every identity considered and not admitted, with why.
        mapping_fingerprint: The instrument mapping this was derived from. The
            universe is a function of that mapping and of nothing else, so a
            universe whose mapping fingerprint does not match the mapping in
            force was derived from different evidence.
        provider: Which venue.
        environment: `PAPER`.
        evidence_at: When the provider was read.
        source_universe: The research cross-section the candidates came from.
        source_universe_as_of: Its date.
    """

    members: tuple[tuple[str, int], ...]
    exclusions: tuple[DeploymentExclusion, ...]
    mapping_fingerprint: str
    provider: str
    environment: str
    evidence_at: str
    source_universe: str
    source_universe_as_of: str

    @property
    def size(self) -> int:
        """How many identities are deployable."""
        return len(self.members)

    @property
    def keys(self) -> frozenset[tuple[str, int]]:
        """Membership as a set, for the intersection the strategy performs."""
        return frozenset(self.members)

    def contains(self, identity: Any) -> bool:
        """Whether an identity may be deployed to.

        Args:
            identity: A `SecurityIdentity`, or anything carrying `kind` and
                `identity_id`.

        Returns:
            True when the identity is a member. Read on `(kind, identity_id)`
            only — a matching symbol on a different identity is not a match.
        """
        return (str(identity.kind), int(identity.identity_id)) in self.keys

    def exclusion_for(self, identity: Any) -> str | None:
        """Why an identity is not deployable.

        Args:
            identity: A `SecurityIdentity`, or anything carrying `kind` and
                `identity_id`.

        Returns:
            The reason, or `None` when the identity is a member. An identity the
            mapping never considered also returns `Routability.UNMAPPED`: it has
            no venue evidence, which is the same position as one whose symbol
            nothing answered for.
        """
        key = (str(identity.kind), int(identity.identity_id))
        if key in self.keys:
            return None
        for row in self.exclusions:
            if (row.kind, row.identity_id) == key:
                return row.reason
        from .deployment import Routability

        return Routability.UNMAPPED

    def by_reason(self) -> dict[str, int]:
        """How many identities each exclusion reason accounts for."""
        counts: dict[str, int] = {}
        for row in self.exclusions:
            counts[row.reason] = counts.get(row.reason, 0) + 1
        return dict(sorted(counts.items()))

    def body(self) -> dict[str, Any]:
        """The universe's semantic content, for fingerprinting.

        Every member identity is included, not a count: the fingerprint has to
        change when membership changes, and a digest over counts alone would be
        identical for two universes that swapped one company for another.
        """
        return {
            "version": DEPLOYMENT_UNIVERSE_VERSION,
            "provider": self.provider,
            "environment": self.environment,
            "evidence_at": self.evidence_at,
            "source_universe": self.source_universe,
            "source_universe_as_of": self.source_universe_as_of,
            "mapping_fingerprint": self.mapping_fingerprint,
            "rule": "ROUTABLE_AND_PROVIDER_STATED_FRACTIONABLE_V1",
            "members": [[kind, identity_id] for kind, identity_id in self.members],
            "excluded_counts": self.by_reason(),
        }

    def fingerprint(self) -> str:
        """A digest over membership and the evidence it came from."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the universe as a JSON-safe mapping."""
        return {
            **self.body(),
            "universe_fingerprint": self.fingerprint(),
            "size": self.size,
            "considered": self.size + len(self.exclusions),
            "exclusions": [row.body() for row in self.exclusions],
            "limitations": [
                "A deployment universe is venue evidence at `evidence_at`, not a "
                "timeless fact. An instrument that becomes fractionable, or stops "
                "being so, makes this fingerprint stale.",
                "Exclusion here is an execution-capability statement and never a "
                "research judgement. A company excluded as WHOLE_SHARE_ONLY may be "
                "fully Compounder-eligible, and its score is unaffected.",
                "No value here was derived from returns. Fractionability was read "
                "from provider metadata and nothing was selected for performance.",
            ],
        }


def fractionable_universe(mapping: InstrumentMapping) -> DeploymentUniverse:
    """Derive the deployment universe from an instrument mapping.

    Mechanical: the rule is `routable AND provider-stated fractionable`, applied
    to every record in the mapping. Nothing is added that the mapping does not
    contain and no record is admitted on a default — `fractionable` must be
    exactly `True`, so both `False` and an unstated capability exclude.

    Args:
        mapping: The instrument mapping. Provider evidence.

    Returns:
        The universe, members in ascending identity order.
    """
    members: list[tuple[str, int]] = []
    exclusions: list[DeploymentExclusion] = []
    for record in mapping.records:
        reason = UniverseExclusion.for_record(record)
        if reason is None:
            members.append((record.kind, record.identity_id))
        else:
            exclusions.append(
                DeploymentExclusion(kind=record.kind, identity_id=record.identity_id, reason=reason)
            )
    return DeploymentUniverse(
        members=tuple(sorted(members)),
        exclusions=tuple(sorted(exclusions, key=lambda row: (row.kind, row.identity_id))),
        mapping_fingerprint=mapping.fingerprint(),
        provider=mapping.provider,
        environment=mapping.environment,
        evidence_at=mapping.evidence_at,
        source_universe=mapping.universe,
        source_universe_as_of=mapping.universe_as_of,
    )
