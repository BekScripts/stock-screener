"""The deployment authority a paper runtime needs, and what only an owner can set.

P2.3.1D wired `run-due` to the submission authority and, in doing so, exposed
two inputs that exist nowhere: a risk policy with actual limits, and a mapping
from durable internal identity to a verified venue instrument. Neither can be
derived from code, and the repository is explicit that the first cannot be
derived at all — `EXAMPLE_POLICY_V1` carries `production_authorized=False` and
its own docstring reserves promotion for "a dedicated risk-policy phase with its
own evidence".

So this module is deliberately split along the line between what *can* be
established and what cannot:

- **The instrument mapping is evidence.** Every field in it is read from the
  authoritative internal identity source or from the venue's own asset
  metadata. Nothing is chosen, so the artefact can be built and fingerprinted
  without anybody's approval.
- **The risk policy is judgment.** Concentration, cash floor, volatility
  ceiling and drawdown ladder are statements about how much of a mistake the
  owner is willing to make. This module therefore emits a *candidate* that
  names each unresolved field, carries no invented number, and refuses to call
  itself authorised.

The two are kept apart because conflating them is how an example policy becomes
production authority: a composition root that needs *a* policy will accept any
policy, and the plausible-looking number is the one that gets used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "INSTRUMENT_MAPPING_VERSION",
    "NO_CAP",
    "OWNER_APPROVAL_REQUIRED",
    "OWNER_APPROVED_PAPER_LIMITS",
    "PAPER_RISK_POLICY_VERSION",
    "REBIND_PLAN_VERSION",
    "RISK_POLICY_CANDIDATE_VERSION",
    "InstrumentMapping",
    "InstrumentRecord",
    "PaperRiskPolicy",
    "PolicyField",
    "RebindPlan",
    "RiskPolicyCandidate",
    "Routability",
    "mapping_from_evidence",
    "paper_risk_policy",
    "policy_field_inventory",
    "rebind_plan",
    "risk_policy_candidate",
]

#: The candidate risk-policy contract. `CANDIDATE` is part of the name on
#: purpose: an artefact that reads as authorised is one somebody will treat as
#: authorised.
RISK_POLICY_CANDIDATE_VERSION = "PAPER_PORTFOLIO_RISK_POLICY_V1_CANDIDATE"

#: The instrument mapping contract.
INSTRUMENT_MAPPING_VERSION = "ALPACA_PAPER_INSTRUMENT_MAPPING_V1"

#: The rebind plan contract.
REBIND_PLAN_VERSION = "PAPER_OPERATIONS_RUNTIME_REBIND_PLAN_V1"

#: What a field's value is when no authority for it exists. A sentinel rather
#: than `None`, because `None` is a legitimate policy value meaning "no cap"
#: and the two must never be confused: "the owner declined to cap this" and
#: "nobody has decided" are different states.
OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"


class Routability:
    """Why an identity may or may not be routed to the venue."""

    ROUTABLE = "ROUTABLE"
    """Unambiguous identity, active and tradable, capability known."""

    UNMAPPED = "UNMAPPED"
    """No venue asset answered for this identity's symbol."""

    AMBIGUOUS = "AMBIGUOUS"
    """More than one durable identity claims this symbol."""

    INACTIVE = "INACTIVE"
    """The venue holds the asset but it is not active."""

    NOT_TRADABLE = "NOT_TRADABLE"
    """Active at the venue and not tradable."""

    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    """The venue answered for a different symbol than was asked for."""

    FRACTIONABILITY_UNKNOWN = "FRACTIONABILITY_UNKNOWN"
    """Active and tradable, but the venue did not state fractionability. Its own
    outcome rather than `NOT_TRADABLE`: the asset *is* tradable and what is
    missing is a capability, so reporting it as untradable would misdescribe
    both the venue and the reason."""


@dataclass(frozen=True)
class PolicyField:
    """One risk-policy field, and whether anything authorises a value for it.

    Attributes:
        name: The `RiskPolicy` field.
        unit: What the number means.
        meaning: What it constrains.
        engine_default: What the engine does with no value supplied.
        constraint: `HARD` when the engine refuses a breach outright, `SOFT`
            when it reduces to fit.
        interacts_with: Other fields whose effect compounds with this one.
        authority: Where an authorised value comes from, or
            `OWNER_APPROVAL_REQUIRED`.
        candidate: A conservatively justifiable value, where one can be argued
            without choosing a risk appetite. `OWNER_APPROVAL_REQUIRED`
            otherwise.
        rationale: Why that candidate, or why none can be derived.
    """

    name: str
    unit: str
    meaning: str
    engine_default: str
    constraint: str
    interacts_with: tuple[str, ...]
    authority: str
    candidate: Any
    rationale: str

    @property
    def needs_owner(self) -> bool:
        """Whether this field cannot be settled without an owner decision."""
        return self.authority == OWNER_APPROVAL_REQUIRED

    def as_json(self) -> dict[str, Any]:
        """Return the field as a JSON-safe mapping."""
        return {
            "name": self.name,
            "unit": self.unit,
            "meaning": self.meaning,
            "engine_default": self.engine_default,
            "constraint": self.constraint,
            "interacts_with": list(self.interacts_with),
            "authority": self.authority,
            "candidate": self.candidate,
            "needs_owner_approval": self.needs_owner,
            "rationale": self.rationale,
        }


def policy_field_inventory() -> tuple[PolicyField, ...]:
    """Every `RiskPolicy` field that can materially change approved exposure.

    The §3 inventory. Three fields can be settled without an owner decision and
    the rest cannot, and the division is worth stating precisely because it is
    the whole approval surface:

    - A field is **derivable** when the repository already constrains it. "No
      leverage is authorised anywhere" bounds gross exposure at 1.0; "the
      strategy emits only positive weights" settles `long_only`; "missing is
      not zero" settles the three missing-metric policies.
    - A field is **owner judgment** when its value expresses how much of a loss
      is acceptable. No amount of reading the repository produces a position
      cap, a cash floor or a drawdown ladder, and deriving one from historical
      return would be the parameter fitting this programme exists to avoid.

    Returns:
        The inventory, in `RiskPolicy` field order.
    """
    return (
        PolicyField(
            name="policy_id",
            unit="identifier",
            meaning="which policy this is",
            engine_default="required",
            constraint="IDENTITY",
            interacts_with=(),
            authority="ALPHATRADER_PAPER — the operational policy identity the "
            "V2 soak protocol already bound",
            candidate="ALPHATRADER_PAPER",
            rationale="not a limit; the identity is already recorded in the frozen "
            "V2 protocol's risk_policy_fingerprint body",
        ),
        PolicyField(
            name="policy_version",
            unit="identifier",
            meaning="its version; a changed limit is a new version",
            engine_default="required",
            constraint="IDENTITY",
            interacts_with=(),
            authority="derived — V1 is the first",
            candidate="V1",
            rationale="not a limit",
        ),
        PolicyField(
            name="mode",
            unit="enum",
            meaning="what the policy currently permits",
            engine_default="NORMAL",
            constraint="HARD",
            interacts_with=("drawdown_bands",),
            authority="derived — NORMAL is the engine's unrestricted mode and any "
            "other mode is a restriction nobody has asked for",
            candidate="NORMAL",
            rationale="the restrictive modes exist for a drawdown overlay to select; "
            "setting one as the standing mode would be a risk decision",
        ),
        PolicyField(
            name="construction_mode",
            unit="enum",
            meaning="how an underinvested request is treated",
            engine_default="PRESERVE_ABSOLUTE_WEIGHTS",
            constraint="HARD",
            interacts_with=("max_gross_exposure", "min_cash_weight"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate="PRESERVE_ABSOLUTE_WEIGHTS",
            rationale="materially changes exposure: the alternative scales an "
            "underinvested request up to the gross cap, which turns the strategy's "
            "own sizing into something else. The engine default is the "
            "non-amplifying choice, but choosing not to amplify is still a choice "
            "about how much capital is deployed",
        ),
        PolicyField(
            name="long_only",
            unit="boolean",
            meaning="whether negative target weights are refused",
            engine_default="True",
            constraint="HARD",
            interacts_with=("max_net_exposure",),
            authority="derived — ALPHATRADER_COMPOUNDER_MONTHLY_V1 emits only "
            "positive weights, and no short-selling authority exists; the certified "
            "binding's capabilities declare short_selling=False",
            candidate=True,
            rationale="permitting shorts would grant an authority neither the "
            "strategy nor the venue binding has",
        ),
        PolicyField(
            name="max_gross_exposure",
            unit="fraction of NAV",
            meaning="cap on the sum of absolute target weights",
            engine_default="1.0",
            constraint="SOFT — the engine reduces to fit",
            interacts_with=("min_cash_weight", "max_position_weight", "max_positions"),
            authority="bounded above at 1.0 — no leverage or margin authority "
            "exists anywhere in the repository, and >1.0 requires borrowing",
            candidate=1.0,
            rationale="1.0 is the most permissive value that needs no leverage "
            "authority: it means 'never borrow'. Any value below 1.0 is a "
            "deliberate underinvestment and is owner judgment, so the bound is "
            "derived and the choice to sit exactly at it is not a risk appetite",
        ),
        PolicyField(
            name="max_net_exposure",
            unit="fraction of NAV or None",
            meaning="cap on the sum of signed target weights",
            engine_default="None (no cap)",
            constraint="SOFT",
            interacts_with=("long_only", "max_gross_exposure"),
            authority="derived — under long_only every weight is positive, so net "
            "equals gross and a separate net cap can bind nothing tighter",
            candidate=1.0,
            rationale="redundant with the gross cap for a long-only book; stated "
            "explicitly rather than left None so the artefact records that net "
            "exposure was considered",
        ),
        PolicyField(
            name="max_position_weight",
            unit="fraction of NAV",
            meaning="cap on any single target weight",
            engine_default="0.10",
            constraint="SOFT",
            interacts_with=("max_positions", "max_gross_exposure"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="single-name concentration is the most consequential limit "
            "here and nothing in the repository establishes one. The engine's 0.10 "
            "default comes from EXAMPLE_POLICY_V1, which is explicitly not "
            "authorised; adopting it would promote an example by reuse",
        ),
        PolicyField(
            name="max_positions",
            unit="count",
            meaning="cap on the number of held positions",
            engine_default="20",
            constraint="HARD",
            interacts_with=("max_position_weight", "min_position_weight"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="the strategy's own TARGET_COUNT_RULE selects "
            "ceil(eligible/10), which on the current cross-section of 1896 eligible "
            "names is 190. A cap below that silently truncates the strategy's "
            "selection and a cap above it binds nothing — either is a decision "
            "about how diversified the book should be",
        ),
        PolicyField(
            name="min_cash_weight",
            unit="fraction of NAV",
            meaning="cash that must remain unallocated",
            engine_default="0.05",
            constraint="SOFT",
            interacts_with=("max_gross_exposure", "construction_mode"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="a cash buffer trades expected return for settlement and "
            "rebalancing headroom. 0.0 and 0.05 are both defensible and the "
            "repository prefers neither",
        ),
        PolicyField(
            name="max_trade_notional_weight",
            unit="fraction of NAV or None",
            meaning="cap on a single trade as a fraction of NAV",
            engine_default="None (no cap)",
            constraint="SOFT",
            interacts_with=("max_position_weight",),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="bounds how fast the book may move in one session; no "
            "turnover or trade-size authority exists",
        ),
        PolicyField(
            name="min_position_weight",
            unit="fraction of NAV or None",
            meaning="targets below this are dropped rather than held",
            engine_default="None (keep every target)",
            constraint="HARD",
            interacts_with=("max_positions",),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="drops the strategy's smallest selections, so it changes "
            "which names are held and not merely how much",
        ),
        PolicyField(
            name="max_portfolio_volatility",
            unit="annualised fraction or None",
            meaning="ceiling on estimated annualised portfolio volatility",
            engine_default="None (no ceiling)",
            constraint="SOFT",
            interacts_with=("missing_volatility_policy", "max_risk_metric_age_seconds"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="requires both a risk appetite and a covariance source this "
            "runtime does not currently supply; leaving it None is itself a "
            "decision to run without a volatility ceiling",
        ),
        PolicyField(
            name="max_position_adv_fraction",
            unit="fraction of average daily volume or None",
            meaning="a position may not exceed this fraction of the instrument's ADV",
            engine_default="None (no cap)",
            constraint="SOFT",
            interacts_with=("missing_liquidity_policy", "max_trade_adv_fraction"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="a liquidity limit. `companies.average_volume` exists as an "
            "input but no authorised fraction of it does",
        ),
        PolicyField(
            name="max_trade_adv_fraction",
            unit="fraction of average daily volume or None",
            meaning="a single change in exposure may not exceed this fraction of ADV",
            engine_default="None (no cap)",
            constraint="SOFT",
            interacts_with=("missing_liquidity_policy", "max_position_adv_fraction"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="as above, for the trade rather than the resulting position",
        ),
        PolicyField(
            name="drawdown_bands",
            unit="ladder of (threshold, mode) bands",
            meaning="the drawdown overlay ladder, shallowest first",
            engine_default="() — no overlay",
            constraint="HARD",
            interacts_with=("mode",),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="every band is a pair of owner decisions — at what loss, and "
            "what to restrict. An empty ladder means the book never de-risks on "
            "drawdown, which is also a decision",
        ),
        PolicyField(
            name="missing_volatility_policy",
            unit="enum",
            meaning="what an unknown volatility or absent covariance permits",
            engine_default="REJECT_INCREASE",
            constraint="HARD",
            interacts_with=("max_portfolio_volatility",),
            authority="derived — the repository's 'missing is not zero' rule, and "
            "REJECT_INCREASE is the conservative member",
            candidate="REJECT_INCREASE",
            rationale="an unknown risk metric must not permit a larger position; "
            "the permissive alternative treats absent evidence as safe",
        ),
        PolicyField(
            name="missing_liquidity_policy",
            unit="enum",
            meaning="what an unknown ADV permits",
            engine_default="REJECT_INCREASE",
            constraint="HARD",
            interacts_with=("max_position_adv_fraction", "max_trade_adv_fraction"),
            authority="derived — as above",
            candidate="REJECT_INCREASE",
            rationale="as above",
        ),
        PolicyField(
            name="max_risk_metric_age_seconds",
            unit="seconds or None",
            meaning="how stale a risk metric may be",
            engine_default="None (no bound)",
            constraint="HARD",
            interacts_with=(
                "missing_volatility_policy",
                "missing_liquidity_policy",
                "missing_correlation_policy",
            ),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="a freshness bound, and the operational freshness policies "
            "elsewhere are per-input rather than a single risk-metric age; no "
            "existing value transfers",
        ),
        PolicyField(
            name="correlation_threshold",
            unit="signed correlation or None",
            meaning="the correlation at or above which a pair is concentrated",
            engine_default="None (no pair control)",
            constraint="SOFT",
            interacts_with=("max_correlated_pair_exposure", "missing_correlation_policy"),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="PROJECT_STATE records production correlation thresholds as "
            "NOT AUTHORIZED, and this runtime supplies no covariance source",
        ),
        PolicyField(
            name="max_correlated_pair_exposure",
            unit="fraction of NAV or None",
            meaning="the greatest combined weight a concentrated pair may hold",
            engine_default="None (no cap)",
            constraint="SOFT",
            interacts_with=("correlation_threshold",),
            authority=OWNER_APPROVAL_REQUIRED,
            candidate=OWNER_APPROVAL_REQUIRED,
            rationale="meaningless without a threshold, and equally unauthorised",
        ),
        PolicyField(
            name="missing_correlation_policy",
            unit="enum",
            meaning="what an undefined correlation permits",
            engine_default="REJECT_INCREASE",
            constraint="HARD",
            interacts_with=("correlation_threshold",),
            authority="derived — as the other missing-metric policies",
            candidate="REJECT_INCREASE",
            rationale="as above",
        ),
        PolicyField(
            name="production_authorized",
            unit="boolean",
            meaning="whether these limits have been established as correct",
            engine_default="False",
            constraint="DECLARATION",
            interacts_with=(),
            authority="derived — False, because the fields above are unresolved",
            candidate=False,
            rationale="the flag is evidence carried into every risk report, not a "
            "gate. Setting it True while any limit is unresolved would put a false "
            "declaration into the record",
        ),
    )


@dataclass(frozen=True)
class RiskPolicyCandidate:
    """A paper risk policy with its unresolved fields named rather than filled.

    It is not a `RiskPolicy` and cannot be handed to the engine. That is the
    point: a candidate that could be used would be used. `authorised_policy`
    builds the real thing, and only once every field the inventory marks as
    owner judgment has been supplied by an owner.

    Attributes:
        policy_id: The operational policy identity.
        policy_version: Its version.
        environment: `EXTERNAL_PAPER`. Never a live environment.
        engine_version: The engine these fields belong to.
        fields: The inventory, with candidates where one is derivable.
        authorized: Always False here.
    """

    policy_id: str
    policy_version: str
    environment: str
    engine_version: str
    fields: tuple[PolicyField, ...]
    authorized: bool = False

    @property
    def owner_approval_required(self) -> tuple[str, ...]:
        """The finite set of fields an owner has to decide."""
        return tuple(row.name for row in self.fields if row.needs_owner)

    @property
    def derivable(self) -> tuple[str, ...]:
        """The fields the repository already constrains."""
        return tuple(row.name for row in self.fields if not row.needs_owner)

    def body(self) -> dict[str, Any]:
        """The candidate's semantic content, for fingerprinting.

        Every material field is in here, so changing one limit — or resolving
        one — changes the fingerprint. The field list is sorted by name, so the
        order it was assembled in cannot affect the digest.
        """
        return {
            "version": RISK_POLICY_CANDIDATE_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "environment": self.environment,
            "engine_version": self.engine_version,
            "authorized": self.authorized,
            "fields": [
                {"name": row.name, "candidate": row.candidate, "authority": row.authority}
                for row in sorted(self.fields, key=lambda row: row.name)
            ],
        }

    def fingerprint(self) -> str:
        """A digest over every material field and the authorisation state."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the candidate as a JSON-safe mapping."""
        return {
            **self.body(),
            "owner_approval_required": list(self.owner_approval_required),
            "derivable": list(self.derivable),
            "inventory": [row.as_json() for row in self.fields],
            "policy_fingerprint": self.fingerprint(),
            "limitations": [
                "NOT_AUTHORIZED: this is a candidate. Every field named in "
                "owner_approval_required is unresolved and no value for it appears "
                "anywhere in this repository.",
                "EXTERNAL_PAPER only. No live authority is expressed or implied.",
                "The engine's own defaults are not authority: they come from "
                "EXAMPLE_POLICY_V1, which declares production_authorized=False.",
                "No limit here was chosen by measuring returns. Fitting a risk "
                "limit to historical performance is the failure this programme "
                "exists to avoid.",
            ],
        }

    def authorised_policy(self, **approved: Any) -> Any:
        """Build the real `RiskPolicy` once an owner has supplied the rest.

        Args:
            **approved: A value for every field in `owner_approval_required`.

        Returns:
            The `RiskPolicy`, with `production_authorized` still False — that
            flag is the owner's declaration to make in the authored artefact,
            not this function's to grant.

        Raises:
            RuntimeBindingError: `RUNTIME_RISK_POLICY_NOT_AUTHORED` if any
                owner-judgment field is absent. Partial approval is refused
                rather than completed from the engine's defaults, because a
                default that fills an unapproved limit is the exact failure
                this artefact exists to prevent.
        """
        from stock_screener.portfolio_risk import RiskPolicy

        from .errors import Code, RuntimeBindingError

        missing = [name for name in self.owner_approval_required if name not in approved]
        if missing:
            raise RuntimeBindingError(
                Code.RUNTIME_RISK_POLICY_NOT_AUTHORED,
                f"{len(missing)} risk field(s) still need owner approval: {missing}. "
                "They are not filled from the engine's defaults: those come from an "
                "example policy that declares itself unauthorised",
            )
        settings: dict[str, Any] = {
            row.name: row.candidate for row in self.fields if not row.needs_owner
        }
        settings.update(approved)
        settings.pop("production_authorized", None)
        return RiskPolicy(**settings, production_authorized=False)


def risk_policy_candidate() -> RiskPolicyCandidate:
    """The paper risk-policy candidate for this build.

    Returns:
        The candidate. Unauthorised by construction.
    """
    from stock_screener.execution.versions import EXECUTION_ENGINE_VERSION

    _ = EXECUTION_ENGINE_VERSION
    return RiskPolicyCandidate(
        policy_id="ALPHATRADER_PAPER",
        policy_version="V1",
        environment="EXTERNAL_PAPER",
        engine_version="PORTFOLIO_RISK_ENGINE_V1_2",
        fields=policy_field_inventory(),
        authorized=False,
    )


@dataclass(frozen=True)
class InstrumentRecord:
    """One durable identity, and the venue instrument it routes to.

    Attributes:
        kind: The identity kind. `COMPANY` for an equity in this universe.
        identity_id: The durable internal id — the `companies` primary key.
            Never a ticker: a ticker is reassigned when a company delists and
            another takes the symbol, and two identities that happened to share
            one would become one position.
        internal_symbol: The routing symbol the authoritative internal record
            currently carries. Metadata, not identity.
        provider_asset_id: The venue's own stable asset id, where it gives one.
            Stronger than the symbol: it survives a symbol change and
            distinguishes a reused symbol from the original instrument.
        provider_symbol: The symbol the venue answered with.
        asset_class: The venue's asset class.
        exchange: The listing venue the provider reports.
        status: The provider's asset status.
        tradable: Whether the venue permits trading it.
        fractionable: Whether the venue permits a non-integer quantity. Read
            from provider metadata and never inferred from the fact that the
            strategy produces fractional quantities.
        routability: Why this identity is or is not routable.
        detail: What was found, where a reader needs it.
    """

    kind: str
    identity_id: int
    internal_symbol: str
    provider_asset_id: str
    provider_symbol: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    fractionable: bool | None
    routability: str
    detail: str = ""

    @property
    def label(self) -> str:
        """The identity label the rest of the system uses."""
        return f"{self.kind}:{self.identity_id}"

    @property
    def routable(self) -> bool:
        """Whether an order for this identity may be routed."""
        return self.routability == Routability.ROUTABLE

    def body(self) -> dict[str, Any]:
        """The record's semantic content, for fingerprinting."""
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "internal_symbol": self.internal_symbol,
            "provider_asset_id": self.provider_asset_id,
            "provider_symbol": self.provider_symbol,
            "asset_class": self.asset_class,
            "exchange": self.exchange,
            "status": self.status,
            "tradable": self.tradable,
            "fractionable": self.fractionable,
            "routability": self.routability,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the record as a JSON-safe mapping."""
        return {**self.body(), "detail": self.detail}

    def as_venue_symbol(self) -> Any:
        """The `VenueSymbol` the certified adapter routes with.

        Raises:
            RuntimeBindingError: `RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED` for a
                record that is not routable. A non-routable identity has no
                venue symbol, and producing one anyway is the ticker fallback
                this artefact exists to prevent.
        """
        from stock_screener.execution.external import VenueSymbol

        from .errors import Code, RuntimeBindingError

        if not self.routable:
            raise RuntimeBindingError(
                Code.RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED,
                f"{self.label} is {self.routability}: {self.detail}. There is no "
                "fallback to its internal symbol — routing an order on a symbol the "
                "venue did not confirm is how an order reaches the wrong instrument",
            )
        return VenueSymbol(
            kind=self.kind,
            identity_id=self.identity_id,
            symbol=self.provider_symbol,
            tradable=self.tradable,
            asset_class=self.asset_class,
            fractionable=bool(self.fractionable),
        )


@dataclass(frozen=True)
class InstrumentMapping:
    """Every candidate identity, and whether the venue confirmed it.

    Attributes:
        records: One per candidate identity, routable or not.
        universe: What set the candidates were drawn from.
        universe_as_of: The cross-section the universe was read from.
        provider: Which venue.
        environment: `PAPER`.
        evidence_at: When the provider was read. Recorded because a mapping is
            a statement about the venue at an instant, not a timeless fact.
        provider_evidence_fingerprint: A digest of the provider payload the
            records were built from.
        account_binding_fingerprint: Which account the read was made against,
            as a fingerprint. Never an account number.
    """

    records: tuple[InstrumentRecord, ...]
    universe: str
    universe_as_of: str
    provider: str
    environment: str
    evidence_at: str
    provider_evidence_fingerprint: str
    account_binding_fingerprint: str = ""

    def routable(self) -> tuple[InstrumentRecord, ...]:
        """Only the identities an order may be routed for."""
        return tuple(row for row in self.records if row.routable)

    def by_routability(self) -> dict[str, int]:
        """How many identities fell into each outcome."""
        counts: dict[str, int] = {}
        for row in self.records:
            counts[row.routability] = counts.get(row.routability, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def coverage(self) -> float:
        """The fraction of candidate identities that are routable."""
        if not self.records:
            return 0.0
        return len(self.routable()) / len(self.records)

    def body(self) -> dict[str, Any]:
        """The mapping's semantic content, for fingerprinting.

        Records are sorted by identity, so the order they were assembled in
        cannot change the digest. Every field that would change where an order
        goes — symbol, provider asset id, tradable, fractionable, status — is
        inside it.
        """
        return {
            "version": INSTRUMENT_MAPPING_VERSION,
            "universe": self.universe,
            "universe_as_of": self.universe_as_of,
            "provider": self.provider,
            "environment": self.environment,
            "records": [
                row.body()
                for row in sorted(self.records, key=lambda row: (row.kind, row.identity_id))
            ],
        }

    def fingerprint(self) -> str:
        """A digest over the whole canonical mapping."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the mapping as a JSON-safe mapping."""
        return {
            **self.body(),
            "evidence_at": self.evidence_at,
            "provider_evidence_fingerprint": self.provider_evidence_fingerprint,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "mapping_fingerprint": self.fingerprint(),
            "counts": self.by_routability(),
            "candidate_identities": len(self.records),
            "routable": len(self.routable()),
            "coverage": round(self.coverage, 6),
            "limitations": [
                "A mapping is evidence about the venue at `evidence_at`, not a "
                "timeless fact. An instrument that is reclassified, delisted or "
                "renamed afterwards makes this fingerprint stale.",
                "Fractionability is provider metadata. A null value is recorded as "
                "unknown and the identity is not routable: an unknown capability is "
                "never read as permitted.",
                "A non-routable identity has no venue symbol at all. There is no "
                "fallback to the internal symbol.",
            ],
        }

    def venue_mapping(self) -> Any:
        """The `SymbolMapping` the certified adapter routes with.

        Only routable records are included, so an identity the venue did not
        confirm cannot be routed at all rather than being routed on a guess.
        """
        from stock_screener.execution.external import SymbolMapping

        return SymbolMapping.of(row.as_venue_symbol() for row in self.routable())

    def authored_symbols(self) -> list[dict[str, Any]]:
        """The rows an operator would author into `runtime/symbol_mapping.json`."""
        return [
            {
                "kind": row.kind,
                "identity_id": row.identity_id,
                "symbol": row.provider_symbol,
                "fractionable": bool(row.fractionable),
                "provider_asset_id": row.provider_asset_id,
            }
            for row in self.routable()
        ]


def mapping_from_evidence(
    *,
    identities: dict[int, str],
    provider_assets: dict[str, dict[str, Any]],
    universe: str,
    universe_as_of: str,
    evidence_at: str,
    provider_evidence_fingerprint: str,
    provider: str = "ALPACA",
    environment: str = "PAPER",
    account_binding_fingerprint: str = "",
    kind: str = "COMPANY",
) -> InstrumentMapping:
    """Join durable identities to provider asset evidence.

    Neither side is trusted to settle the other. The internal record supplies
    the identity and its current routing symbol; the venue supplies everything
    about the instrument. Where they disagree, or where either is silent, the
    identity is not routable — there is no step here that falls back to the
    internal symbol.

    Ambiguity is refused rather than resolved. Two durable identities carrying
    one symbol is the case a ticker-keyed mapping gets silently wrong: both
    would route to one instrument and their positions would merge. Both are
    marked `AMBIGUOUS` and neither is routable.

    Args:
        identities: Durable identity id to its current internal symbol, from
            the authoritative internal record.
        provider_assets: Upper-cased provider symbol to the venue's asset
            payload.
        universe: What set the candidates were drawn from.
        universe_as_of: The cross-section date.
        evidence_at: When the provider was read.
        provider_evidence_fingerprint: A digest of that payload.
        provider: The venue.
        environment: `PAPER`.
        account_binding_fingerprint: Which account, as a fingerprint.
        kind: The identity kind.

    Returns:
        The mapping, with every candidate accounted for.
    """
    claimed: dict[str, list[int]] = {}
    for identity_id, symbol in identities.items():
        claimed.setdefault(symbol.strip().upper(), []).append(identity_id)

    records: list[InstrumentRecord] = []
    for identity_id, raw in sorted(identities.items()):
        symbol = raw.strip().upper()
        shared = claimed.get(symbol, [])
        asset = provider_assets.get(symbol)
        stated = None if asset is None else asset.get("fractionable")

        def build(
            routability: str,
            detail: str,
            *,
            asset: dict[str, Any] | None = asset,
            identity_id: int = identity_id,
            symbol: str = symbol,
            stated: Any = stated,
        ) -> InstrumentRecord:
            """One record, with every provider field read explicitly."""
            return InstrumentRecord(
                kind=kind,
                identity_id=identity_id,
                internal_symbol=symbol,
                provider_asset_id="" if asset is None else str(asset.get("id", "")),
                provider_symbol="" if asset is None else str(asset.get("symbol", "")),
                asset_class="" if asset is None else str(asset.get("class", "")),
                exchange="" if asset is None else str(asset.get("exchange", "")),
                status="" if asset is None else str(asset.get("status", "")),
                tradable=bool(asset.get("tradable", False)) if asset else False,
                fractionable=None if stated is None else bool(stated),
                routability=routability,
                detail=detail,
            )

        if len(shared) > 1:
            records.append(
                build(
                    Routability.AMBIGUOUS,
                    f"{symbol} is claimed by durable identities {sorted(shared)}. "
                    "Neither is routable: routing both to one instrument would "
                    "merge two positions the book keeps apart",
                )
            )
            continue
        if asset is None:
            records.append(build(Routability.UNMAPPED, f"the venue returned no asset for {symbol}"))
            continue
        answered = str(asset.get("symbol", "")).upper()
        if answered != symbol:
            records.append(
                build(
                    Routability.SYMBOL_MISMATCH,
                    f"asked for {symbol} and the venue answered for {answered}",
                )
            )
            continue
        status = str(asset.get("status", ""))
        if status.lower() != "active":
            records.append(build(Routability.INACTIVE, f"the venue reports status {status!r}"))
            continue
        if not bool(asset.get("tradable", False)):
            records.append(
                build(
                    Routability.NOT_TRADABLE,
                    "the venue reports the asset as not tradable",
                )
            )
            continue
        if stated is None:
            records.append(
                build(
                    Routability.FRACTIONABILITY_UNKNOWN,
                    "the venue did not state fractionability. An unknown capability "
                    "is not read as permitted: the strategy sizes by weight, so a "
                    "whole-share-only instrument would be refused by the adapter "
                    "and guessing would send the wrong quantity",
                )
            )
            continue
        records.append(build(Routability.ROUTABLE, ""))
    return InstrumentMapping(
        records=tuple(records),
        universe=universe,
        universe_as_of=universe_as_of,
        provider=provider,
        environment=environment,
        evidence_at=evidence_at,
        provider_evidence_fingerprint=provider_evidence_fingerprint,
        account_binding_fingerprint=account_binding_fingerprint,
    )


@dataclass(frozen=True)
class RebindPlan:
    """What an operator must do to deploy these artefacts, and what they must not.

    The question §28 asks is mechanical, and the answer turns on three facts
    established by reading the contracts rather than by judgement:

    1. `PaperOperationsRuntimeBinding` binds **nothing** about a risk policy, a
       symbol mapping, instrument capabilities or fractionability. Authoring
       either artefact therefore requires no rebind at all — P2.3.1D's
       composition reads both from files under the operations root.
    2. The accounting and execution generations are
       `f"alphatrader-paper-{runtime_id}"`. They are derived from the runtime
       id alone and are **capability-independent**, so widening an instrument
       capability does not of itself touch either lineage.
    3. The one field that must change is `account_binding_fingerprint`, because
       `ExternalBrokerAccountBinding.binding_fingerprint()` digests
       `capabilities`, and the environment proof compares the adapter's binding
       against the recorded value. Fractional execution needs the recorded
       fingerprint to be the widened one.

    So instrument capability is **runtime-binding metadata, not accounting
    semantics** — the contracts do not conflate them, and this plan does not
    either. What forces an operator action is not the ledger; it is that
    `RuntimeBindingStore.write` refuses to repoint a bound root in place and
    `bootstrap_paper_book` refuses a root that already holds a book.

    Attributes:
        outcome: `REBIND_REUSE_EXISTING_LINEAGES` or
            `REBIND_REQUIRES_NEW_GENERATIONS`.
        reason: The exact contract invariant that decides it.
        current_binding_fingerprint: The runtime binding in force.
        policy_fingerprint: The candidate risk policy.
        mapping_fingerprint: The instrument mapping.
        accounting_generation: The ledger lineage to preserve.
        execution_generation: The journal lineage to preserve.
        operations_generation: The operations journal head.
        bootstrap_epoch: The epoch to preserve.
        required_operator_actions: What a human must do, in order.
        resulting_binding_fields: What the new binding would differ by.
        prohibited: What this plan explicitly does not authorise.
    """

    outcome: str
    reason: str
    current_binding_fingerprint: str
    policy_fingerprint: str
    mapping_fingerprint: str
    accounting_generation: str
    execution_generation: str
    operations_generation: str
    bootstrap_epoch: str
    required_operator_actions: tuple[str, ...]
    resulting_binding_fields: tuple[tuple[str, str], ...]
    prohibited: tuple[str, ...]

    def body(self) -> dict[str, Any]:
        """The plan's semantic content, for fingerprinting."""
        return {
            "version": REBIND_PLAN_VERSION,
            "outcome": self.outcome,
            "reason": self.reason,
            "current_binding_fingerprint": self.current_binding_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "mapping_fingerprint": self.mapping_fingerprint,
            "accounting_generation": self.accounting_generation,
            "execution_generation": self.execution_generation,
            "operations_generation": self.operations_generation,
            "bootstrap_epoch": self.bootstrap_epoch,
            "resulting_binding_fields": [list(row) for row in self.resulting_binding_fields],
        }

    def fingerprint(self) -> str:
        """A digest over the plan."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the plan as a JSON-safe mapping."""
        return {
            **self.body(),
            "required_operator_actions": list(self.required_operator_actions),
            "prohibited": list(self.prohibited),
            "plan_fingerprint": self.fingerprint(),
            "executed": False,
        }


def rebind_plan(
    *,
    binding: Any,
    policy_fingerprint: str,
    mapping_fingerprint: str,
    operations_generation: str = "",
    mapping_needs_fractional: bool = True,
) -> RebindPlan:
    """Decide mechanically what deploying these artefacts requires.

    Args:
        binding: The runtime binding in force.
        policy_fingerprint: The candidate risk policy's digest.
        mapping_fingerprint: The instrument mapping's digest.
        operations_generation: The operations journal head, where known.
        mapping_needs_fractional: Whether any routable instrument the mapping
            declares is fractionable. False means the recorded capability
            already covers the mapping and nothing need change.

    Returns:
        The plan. Nothing is mutated.
    """
    actions: list[str] = [
        "author runtime/risk_policy.json — requires owner approval of the "
        "unresolved fields the policy candidate names; no rebind is involved",
        "author runtime/symbol_mapping.json from the verified instrument mapping; "
        "no rebind is involved",
    ]
    if not mapping_needs_fractional:
        return RebindPlan(
            outcome="REBIND_REUSE_EXISTING_LINEAGES",
            reason=(
                "neither artefact is bound by PAPER_OPERATIONS_RUNTIME_BINDING_V1, "
                "and the mapping declares no fractionable instrument, so the "
                "recorded account_binding_fingerprint already describes the "
                "capabilities in use. No field of the binding changes"
            ),
            current_binding_fingerprint=binding.fingerprint(),
            policy_fingerprint=policy_fingerprint,
            mapping_fingerprint=mapping_fingerprint,
            accounting_generation=binding.accounting_generation,
            execution_generation=binding.execution_journal_generation,
            operations_generation=operations_generation,
            bootstrap_epoch=binding.bootstrap_epoch,
            required_operator_actions=tuple(actions),
            resulting_binding_fields=(),
            prohibited=_prohibited(),
        )
    actions.append(
        "decide whether the runtime should be able to send fractional quantities. "
        "If yes, the recorded account_binding_fingerprint must become the digest "
        "of the venue binding whose capabilities declare fractional_quantities="
        "True; if no, execution of this strategy will be refused at the adapter "
        "with BROKER_FRACTIONAL_NOT_SUPPORTED, which is the adapter working"
    )
    actions.append(
        "if fractional capability is wanted: a new runtime binding is required, "
        "because RuntimeBindingStore.write refuses to repoint a bound root in "
        "place (RUNTIME_ALREADY_BOUND) and bootstrap_paper_book refuses a root "
        "that already holds a book (BOOTSTRAP_ALREADY_DONE). This is a contract "
        "refusal about the *binding*, not about the ledger"
    )
    actions.append(
        "the accounting and execution generations are "
        'f"alphatrader-paper-{runtime_id}" and are capability-independent, so a '
        "rebind that keeps the same runtime_id names the same lineages. Preserving "
        "the existing ledger therefore requires rebinding in place rather than "
        "bootstrapping a fresh root — which the current contract does not support "
        "and which this phase does not add"
    )
    return RebindPlan(
        outcome="REBIND_REQUIRES_NEW_GENERATIONS",
        reason=(
            "not because of the policy or the mapping — neither is bound — and not "
            "because of accounting semantics. The binding's "
            "account_binding_fingerprint digests ExternalBrokerAccountBinding."
            "capabilities, the environment proof compares the adapter's binding "
            "against it, and the only contract-supported way to change a bound "
            "field is to bootstrap a new root. bootstrap_paper_book opens a fresh "
            "ledger in that root, so taking the supported path would start a new "
            "accounting history. No in-place rebind exists to preserve it"
        ),
        current_binding_fingerprint=binding.fingerprint(),
        policy_fingerprint=policy_fingerprint,
        mapping_fingerprint=mapping_fingerprint,
        accounting_generation=binding.accounting_generation,
        execution_generation=binding.execution_journal_generation,
        operations_generation=operations_generation,
        bootstrap_epoch=binding.bootstrap_epoch,
        required_operator_actions=tuple(actions),
        resulting_binding_fields=(
            (
                "account_binding_fingerprint",
                "the digest of the venue binding with fractional_quantities=True",
            ),
            ("bootstrap_epoch", "a new epoch, because a bootstrap sets one"),
            ("bootstrap_evidence_fingerprint", "the new account snapshot's digest"),
            ("code_commit", "the build performing the rebind"),
            ("created_at", "when it was performed"),
            (
                "accounting_generation",
                "unchanged if the runtime_id is kept — the generation is derived "
                "from it and is capability-independent",
            ),
            (
                "execution_journal_generation",
                "unchanged for the same reason",
            ),
        ),
        prohibited=_prohibited(),
    )


def _prohibited() -> tuple[str, ...]:
    """What no rebind plan authorises."""
    return (
        "This plan is not an authorisation. It describes operator actions and "
        "performs none of them.",
        "No accounting reset for convenience. A new policy or mapping is not a "
        "reason to replace accumulated internal history with current broker cash.",
        "No paper re-arm. The control state is the operator's to change.",
        "No live authority. EXTERNAL_PAPER only.",
    )


#: The final paper risk policy contract, once an owner has approved its limits.
PAPER_RISK_POLICY_VERSION = "PAPER_PORTFOLIO_RISK_POLICY_V1"

#: The owner's declared intent for a cap that should not bind. Kept as a string
#: so it can never be mistaken for a number: `PORTFOLIO_RISK_ENGINE_V1_2` has no
#: representation for an absent `max_positions`, and the one thing that must not
#: happen is this becoming 0, a large integer, or the current universe size.
NO_CAP = "NO_CAP"

#: The limits the owner approved for EXTERNAL_PAPER in P2.3.1F. Data, not code:
#: every value here was supplied by the owner, none was derived from returns,
#: and the two translations marked below are mechanical restatements of the
#: owner's intent in the engine's own schema.
OWNER_APPROVED_PAPER_LIMITS: dict[str, Any] = {
    # Preserves the selected identities and their relative weights and permits
    # only reduction. Chosen over NORMALIZE_TO_INVESTABLE_BUDGET on evidence:
    # for a request summing below the budget, NORMALIZE scales each name up —
    # measured at 3.17x on a 0.30 request — which would both introduce exposure
    # the strategy did not ask for and trip the frozen
    # RISK_MAY_REDUCE_NEVER_EXPAND_STRATEGY_AUTHORITY_V1 rule.
    "construction_mode": "PRESERVE_ABSOLUTE_WEIGHTS",
    "max_position_weight": 0.02,
    "max_positions": NO_CAP,
    "min_cash_weight": 0.05,
    "max_trade_notional_weight": 0.02,
    # Translation: the owner approved 0.0, meaning no minimum. The engine
    # refuses a non-positive min_position_weight and documents `None` as "keep
    # every target", which is the same instruction in the engine's schema.
    "min_position_weight": None,
    "max_portfolio_volatility": 0.25,
    "max_position_adv_fraction": 0.01,
    "max_trade_adv_fraction": 0.0025,
    # Translation: a drawdown is a non-positive decimal in this schema, and a
    # band carries a multiplier on the base gross limit rather than an absolute
    # cap. With max_gross_exposure 1.0 the multiplier equals the owner's
    # exposure, so 0.05 -> 0.75 becomes (-0.05, 0.75). The mode stays NORMAL on
    # every band: the owner specified exposure caps and nothing else, and
    # imposing REDUCE_ONLY or HALTED would add behaviour they did not ask for.
    "drawdown_bands": (
        (-0.05, 0.75, "drawdown>=5%"),
        (-0.10, 0.50, "drawdown>=10%"),
        (-0.15, 0.25, "drawdown>=15%"),
        (-0.20, 0.00, "drawdown>=20%"),
    ),
    "max_risk_metric_age_seconds": 345600.0,
    "correlation_threshold": 0.85,
    "max_correlated_pair_exposure": 0.03,
}


@dataclass(frozen=True)
class PaperRiskPolicy:
    """The owner-approved paper risk policy, and the one field it cannot express.

    Every limit is stated explicitly. Nothing is left to a default, because the
    engine's defaults come from `EXAMPLE_POLICY_V1` — and omitting a field is
    not harmless: constructing a `RiskPolicy` without `max_positions` silently
    adopts that example's 20, which is the failure mode this artefact exists to
    make impossible.

    Attributes:
        policy_id: The operational policy identity.
        policy_version: Its version.
        environment: `EXTERNAL_PAPER`. The only environment this authorises.
        engine_version: The engine these limits belong to.
        limits: The owner-approved values, as data.
        authorized_for_paper: Whether the owner has approved these limits for
            paper. True once they have.
        authorized_for_live: Always False, and structurally so.
        unrepresentable: Fields the owner approved that the engine cannot
            express. Named rather than quietly substituted.
    """

    policy_id: str
    policy_version: str
    environment: str
    engine_version: str
    limits: dict[str, Any]
    authorized_for_paper: bool
    authorized_for_live: bool = False
    unrepresentable: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        """The policy's semantic content, for fingerprinting."""
        return {
            "version": PAPER_RISK_POLICY_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "environment": self.environment,
            "engine_version": self.engine_version,
            "authorized_for_paper": self.authorized_for_paper,
            "authorized_for_live": self.authorized_for_live,
            "limits": {key: self.limits[key] for key in sorted(self.limits)},
            "unrepresentable": sorted(self.unrepresentable),
        }

    def fingerprint(self) -> str:
        """A digest over every limit and the authorisation state."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the policy as a JSON-safe mapping."""
        return {
            **self.body(),
            "policy_fingerprint": self.fingerprint(),
            "limitations": [
                "EXTERNAL_PAPER only. LIVE, EXTERNAL_LIVE and PRODUCTION_LIVE are "
                "not authorised and live trading remains NOT_IMPLEMENTED.",
                "These are operational safety limits, not return-optimised "
                "parameters. None was fitted to historical performance.",
                "Approving this artefact does not authorise a runtime rebind, a "
                "paper re-arm, or any broker mutation.",
                "No value is inherited from EXAMPLE_POLICY_V1 or from an engine "
                "default: every approved field is stated here explicitly.",
            ],
        }

    def engine_policy(self, **resolve: Any) -> Any:
        """Build the `RiskPolicy` the engine accepts.

        Args:
            **resolve: A value for each field in `unrepresentable`. Supplying
                one is a statement that the owner has accepted a representable
                substitute for their original intent.

        Returns:
            The policy. `production_authorized` is False: that flag describes
            live capital, and this policy is authorised for paper only.

        Raises:
            RuntimeBindingError: `RUNTIME_RISK_POLICY_NOT_AUTHORED` if an
                unrepresentable field is left unresolved. It is *not* filled
                from the engine default — `RiskPolicy` omitting
                `max_positions` silently takes 20 from the example policy, and
                that substitution would be invisible.
        """
        from stock_screener.portfolio_risk import RiskPolicy
        from stock_screener.portfolio_risk.drawdown import DrawdownBand
        from stock_screener.portfolio_risk.policy import ConstructionMode, PolicyMode

        from .errors import Code, RuntimeBindingError

        missing = [name for name in self.unrepresentable if name not in resolve]
        if missing:
            raise RuntimeBindingError(
                Code.RUNTIME_RISK_POLICY_NOT_AUTHORED,
                f"the owner approved {missing} as values "
                f"{[self.limits[name] for name in missing]}, which "
                f"{self.engine_version} cannot represent. They are not filled from "
                "the engine's defaults: omitting max_positions silently adopts "
                "EXAMPLE_POLICY_V1's 20, and a substitution nobody can see is worse "
                "than a refusal",
            )
        settings = dict(self.limits)
        settings.update(resolve)
        settings["construction_mode"] = ConstructionMode(settings["construction_mode"])
        settings["drawdown_bands"] = tuple(
            DrawdownBand(
                threshold=float(threshold),
                max_gross_multiplier=float(multiplier),
                mode=PolicyMode.NORMAL,
                label=str(label),
            )
            for threshold, multiplier, label in settings["drawdown_bands"]
        )
        derived: dict[str, Any] = {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "mode": PolicyMode.NORMAL,
            "long_only": True,
            "max_gross_exposure": 1.0,
            "max_net_exposure": 1.0,
            "production_authorized": False,
        }
        # A caller may override a derived field — a test exercising HALTED, say
        # — but never an owner-approved limit silently: those come from
        # `limits` and `resolve` only names fields the engine cannot express.
        derived.update({key: value for key, value in settings.items() if key in derived})
        settings = {key: value for key, value in settings.items() if key not in derived}
        return RiskPolicy(**derived, **settings)


def paper_risk_policy() -> PaperRiskPolicy:
    """The owner-approved paper risk policy for this build.

    Returns:
        The policy. Authorised for paper; `max_positions` is named as
        unrepresentable rather than substituted.
    """
    return PaperRiskPolicy(
        policy_id="ALPHATRADER_PAPER",
        policy_version="V1",
        environment="EXTERNAL_PAPER",
        engine_version="PORTFOLIO_RISK_ENGINE_V1_2",
        limits=dict(OWNER_APPROVED_PAPER_LIMITS),
        authorized_for_paper=True,
        authorized_for_live=False,
        unrepresentable=("max_positions",),
    )
