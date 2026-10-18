"""Permission for a strategy to be deployed, and to exactly one environment.

Separate from the strategy because a rule and a permission to run that rule are
different facts with different owners. The strategy says what it wants; this says
where somebody agreed it may want it.

The binding is to the strategy's *fingerprint*, not only its name and version.
A name is too coarse — a changed decile rule under the same name would inherit
the old approval — and a version is only as honest as whoever remembered to bump
it. The fingerprint changes whichever way the policy changes, so a modified
strategy stops being authorised without anyone having to notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import Code, StrategyError

__all__ = [
    "AUTHORIZED_ENVIRONMENTS",
    "FORBIDDEN_ENVIRONMENTS",
    "STRATEGY_AUTHORIZATION_CONTRACT",
    "STRATEGY_AUTHORIZATION_CONTRACT_V2",
    "StrategyDeploymentAuthorization",
    "StrategyDeploymentAuthorizationV2",
    "require_authorization",
]

#: The authorization contract.
STRATEGY_AUTHORIZATION_CONTRACT = "STRATEGY_DEPLOYMENT_AUTHORIZATION_V1"

#: The only environment a strategy may be deployed to.
AUTHORIZED_ENVIRONMENTS: frozenset[str] = frozenset({"EXTERNAL_PAPER"})

#: Named rather than merely absent. A reader checking whether live is possible
#: should find the answer here instead of inferring it from a missing branch, and
#: a future edit that tried to add one has to delete a constant that says no.
FORBIDDEN_ENVIRONMENTS: frozenset[str] = frozenset(
    {"LIVE", "EXTERNAL_LIVE", "PRODUCTION_LIVE", "PAPER_THEN_LIVE"}
)


@dataclass(frozen=True)
class StrategyDeploymentAuthorization:
    """One operator's grant for one exact strategy build.

    Attributes:
        authorization_id: A stable name for this grant.
        strategy_id: Which strategy.
        strategy_version: Which version.
        strategy_fingerprint: Which exact policy. The binding that matters.
        environment: Always `EXTERNAL_PAPER`. Refused otherwise.
        code_commit: Which build the operator approved.
        created_at: When, ISO-8601.
        approved_by: Who. Prose, for the record.
        notes: Why. Prose.
    """

    authorization_id: str
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    code_commit: str
    created_at: str
    environment: str = "EXTERNAL_PAPER"
    approved_by: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        """Refuse a grant that could not legitimately exist.

        Raises:
            StrategyError: `STRATEGY_LIVE_NOT_AUTHORIZED` if the environment is
                a live one, named or otherwise; `STRATEGY_NOT_AUTHORIZED` if the
                fingerprint is absent or the environment is unrecognised. A
                wildcard is refused in both directions: there is no environment
                value meaning "any", and an empty fingerprint would authorise
                every future edit of the strategy.
        """
        environment = self.environment.upper()
        if environment in FORBIDDEN_ENVIRONMENTS or "LIVE" in environment:
            raise StrategyError(
                Code.STRATEGY_LIVE_NOT_AUTHORIZED,
                f"{self.environment!r} is not an authorisable environment. Live "
                "trading is NOT_IMPLEMENTED and NOT_AUTHORIZED, and no grant can "
                "create it",
            )
        if environment not in AUTHORIZED_ENVIRONMENTS:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"{self.environment!r} is not a known environment; expected one of "
                f"{sorted(AUTHORIZED_ENVIRONMENTS)}",
            )
        if not self.strategy_fingerprint.strip():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                "a grant must name the exact strategy fingerprint it approves; an "
                "empty fingerprint would authorise every later edit of the strategy",
            )

    def require(
        self, *, strategy_id: str, strategy_version: str, fingerprint: str, environment: str
    ) -> None:
        """Refuse unless this grant covers exactly this build and environment.

        Args:
            strategy_id: The strategy asking.
            strategy_version: Its version.
            fingerprint: Its policy digest.
            environment: Where it wants to run.

        Raises:
            StrategyError: `STRATEGY_LIVE_NOT_AUTHORIZED` if live was requested;
                `STRATEGY_NOT_AUTHORIZED` if the strategy or environment does not
                match; `STRATEGY_AUTHORIZATION_VERSION_MISMATCH` if the names
                match but the policy digest does not — which is the case worth
                having, because it is what catches an edited strategy still
                carrying an old approval.
        """
        requested = environment.upper()
        if requested in FORBIDDEN_ENVIRONMENTS or "LIVE" in requested:
            raise StrategyError(
                Code.STRATEGY_LIVE_NOT_AUTHORIZED,
                f"{environment!r} was requested. Live trading is NOT_IMPLEMENTED and "
                "NOT_AUTHORIZED; no grant and no flag enables it",
            )
        if requested != self.environment.upper():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant covers {self.environment!r}, not {environment!r}",
            )
        if (strategy_id, strategy_version) != (self.strategy_id, self.strategy_version):
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant covers {self.strategy_id}/{self.strategy_version}, not "
                f"{strategy_id}/{strategy_version}",
            )
        if fingerprint != self.strategy_fingerprint:
            raise StrategyError(
                Code.STRATEGY_AUTHORIZATION_VERSION_MISMATCH,
                f"the grant approved policy {self.strategy_fingerprint[:16]}… and this "
                f"strategy is {fingerprint[:16]}…. A modified strategy is not "
                "automatically authorised; approve the new policy explicitly",
            )

    def body(self) -> dict[str, Any]:
        """Return the grant's semantic content. Carries no credential."""
        return {
            "contract": STRATEGY_AUTHORIZATION_CONTRACT,
            "authorization_id": self.authorization_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "environment": self.environment,
            "code_commit": self.code_commit,
            "created_at": self.created_at,
            "approved_by": self.approved_by,
            "notes": self.notes,
            "authorises": "deployment of this exact policy to a paper venue",
            "does_not_authorise": [
                "any order, quantity or security",
                "any sizing decision — the risk engine remains the only sizing authority",
                "live trading, which is NOT_IMPLEMENTED and NOT_AUTHORIZED",
            ],
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the grant."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the grant with its fingerprint."""
        return {**self.body(), "authorization_fingerprint": self.fingerprint()}


#: The authorization contract that binds a deployment universe as well as a
#: strategy. Additive: V1 keeps its exact schema and its fingerprints, and the
#: grant issued under it stays readable as the historical record of what was
#: approved when a universe was not yet part of the granted authority.
STRATEGY_AUTHORIZATION_CONTRACT_V2 = "STRATEGY_DEPLOYMENT_AUTHORIZATION_V2"


@dataclass(frozen=True)
class StrategyDeploymentAuthorizationV2:
    """One operator's grant for one exact strategy over one exact universe.

    V1 bound the strategy's fingerprint and the environment, which is enough
    when a strategy's behaviour is a function of its own rules. It is not enough
    for a strategy whose selectable population is an *input*: V2 of the
    Compounder ranks the intersection of research eligibility and a frozen
    deployment universe, so changing the universe changes which companies are
    eligible and can change the resulting investment decision. A grant that did
    not name the universe would therefore authorise outcomes nobody reviewed.

    So both are bound, and neither has a wildcard. There is no "any compatible
    universe", no "latest", no `None`, and nothing that resolves a universe at
    run time — the exact frozen fingerprint is part of the granted authority,
    and a different one means this grant does not apply. That is the intended
    cost: a venue reclassifying one instrument produces a new universe, and a
    new universe needs a new reviewed authorization.

    Attributes:
        authorization_id: A stable name for this grant.
        strategy_id: Which strategy.
        strategy_version: Which version.
        strategy_fingerprint: Which exact policy.
        deployment_universe_version: The universe contract.
        deployment_universe_fingerprint: Which exact universe. The field V1
            lacked, and the reason this contract exists.
        code_commit: Which build the operator approved.
        created_at: When, ISO-8601.
        environment: Always `EXTERNAL_PAPER`. Refused otherwise.
        authorization_state: Whether this grant is in force. Named rather than
            implied by the record's existence, so a withdrawn grant can be kept
            as evidence instead of deleted.
        approved_by: Who. Prose, for the record.
        notes: Why. Prose.
        supersedes: The authorization fingerprint this replaces, where it
            replaces one. Provenance only — it grants nothing of its own and
            does not promote what it names.
    """

    authorization_id: str
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    deployment_universe_version: str
    deployment_universe_fingerprint: str
    code_commit: str
    created_at: str
    environment: str = "EXTERNAL_PAPER"
    authorization_state: str = "IN_FORCE"
    approved_by: str = ""
    notes: str = ""
    supersedes: str = ""

    def __post_init__(self) -> None:
        """Refuse a grant that could not legitimately exist.

        Raises:
            StrategyError: `STRATEGY_LIVE_NOT_AUTHORIZED` for a live
                environment, named or otherwise; `STRATEGY_NOT_AUTHORIZED` for
                an unrecognised environment, an absent strategy fingerprint, or
                an absent deployment universe. A blank universe is refused for
                the same reason a blank strategy fingerprint is: it would
                authorise every future universe, which is exactly the weakness
                this contract was written to remove.
        """
        environment = self.environment.upper()
        if environment in FORBIDDEN_ENVIRONMENTS or "LIVE" in environment:
            raise StrategyError(
                Code.STRATEGY_LIVE_NOT_AUTHORIZED,
                f"{self.environment!r} is not an authorisable environment. Live "
                "trading is NOT_IMPLEMENTED and NOT_AUTHORIZED, and no grant can "
                "create it",
            )
        if environment not in AUTHORIZED_ENVIRONMENTS:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"{self.environment!r} is not a known environment; expected one of "
                f"{sorted(AUTHORIZED_ENVIRONMENTS)}",
            )
        if not self.strategy_fingerprint.strip():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                "a grant must name the exact strategy fingerprint it approves; an "
                "empty fingerprint would authorise every later edit of the strategy",
            )
        if not self.deployment_universe_fingerprint.strip():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                "a grant must name the exact deployment universe it approves. An "
                "empty universe is a wildcard: the strategy's selectable "
                "population is an input, so authorising every universe would "
                "authorise selections nobody reviewed",
            )
        if not self.deployment_universe_version.strip():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                "a grant must name the deployment-universe contract it approves, "
                "so a fingerprint cannot be matched against a universe of a "
                "different shape",
            )

    @property
    def in_force(self) -> bool:
        """Whether this grant currently authorises anything."""
        return self.authorization_state == "IN_FORCE"

    def require(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        fingerprint: str,
        environment: str,
        universe_fingerprint: str,
    ) -> None:
        """Refuse unless this grant covers exactly this build, universe and venue.

        Args:
            strategy_id: The strategy asking.
            strategy_version: Its version.
            fingerprint: Its policy digest.
            environment: Where it wants to run.
            universe_fingerprint: The deployment universe it would rank over.
                Required, with no default: a caller that could omit it would be
                back to V1's authority, and the omission would be invisible.

        Raises:
            StrategyError: `STRATEGY_LIVE_NOT_AUTHORIZED` if live was requested;
                `STRATEGY_NOT_AUTHORIZED` if the strategy, the environment, the
                universe or the grant's own state does not match;
                `STRATEGY_AUTHORIZATION_VERSION_MISMATCH` if the names match and
                the policy digest does not.
        """
        requested = environment.upper()
        if requested in FORBIDDEN_ENVIRONMENTS or "LIVE" in requested:
            raise StrategyError(
                Code.STRATEGY_LIVE_NOT_AUTHORIZED,
                f"{environment!r} was requested. Live trading is NOT_IMPLEMENTED and "
                "NOT_AUTHORIZED; no grant and no flag enables it",
            )
        if not self.in_force:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant's state is {self.authorization_state!r}, so it "
                "authorises nothing. It is kept as evidence of what was once "
                "approved, not as a permission",
            )
        if requested != self.environment.upper():
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant covers {self.environment!r}, not {environment!r}",
            )
        if (strategy_id, strategy_version) != (self.strategy_id, self.strategy_version):
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant covers {self.strategy_id}/{self.strategy_version}, not "
                f"{strategy_id}/{strategy_version}",
            )
        if fingerprint != self.strategy_fingerprint:
            raise StrategyError(
                Code.STRATEGY_AUTHORIZATION_VERSION_MISMATCH,
                f"the grant approved policy {self.strategy_fingerprint[:16]}… and this "
                f"strategy is {fingerprint[:16]}…. A modified strategy is not "
                "automatically authorised; approve the new policy explicitly",
            )
        if universe_fingerprint != self.deployment_universe_fingerprint:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"the grant approved deployment universe "
                f"{self.deployment_universe_fingerprint[:16]}… and this deployment "
                f"would rank over {universe_fingerprint[:16]}…. The universe decides "
                "which companies are eligible, so a different one can produce a "
                "different portfolio: it is a new deployment and needs its own "
                "review, not an inherited approval",
            )

    def body(self) -> dict[str, Any]:
        """Return the grant's semantic content. Carries no credential."""
        return {
            "contract": STRATEGY_AUTHORIZATION_CONTRACT_V2,
            "authorization_id": self.authorization_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "deployment_universe_version": self.deployment_universe_version,
            "deployment_universe_fingerprint": self.deployment_universe_fingerprint,
            "environment": self.environment,
            "authorization_state": self.authorization_state,
            "code_commit": self.code_commit,
            "created_at": self.created_at,
            "approved_by": self.approved_by,
            "notes": self.notes,
            "supersedes": self.supersedes,
            "authorises": (
                "deployment of this exact policy, over this exact deployment "
                "universe, to a paper venue"
            ),
            "does_not_authorise": [
                "any order, quantity or security",
                "any sizing decision — the risk engine remains the only sizing authority",
                "a different deployment universe, however similar",
                "live trading, which is NOT_IMPLEMENTED and NOT_AUTHORIZED",
            ],
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the grant."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the grant with its fingerprint."""
        return {**self.body(), "authorization_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> StrategyDeploymentAuthorizationV2:
        """Rebuild a grant from its stored body.

        Args:
            body: The stored mapping.

        Returns:
            The grant, which re-digests to the stored fingerprint when nothing
            has been altered.

        Raises:
            StrategyError: `STRATEGY_NOT_AUTHORIZED` for a body of another
                contract version, or one missing a field this contract requires.
                Read field by field rather than splatted, so a field added to
                the writer and not to this reader fails here instead of
                surviving as a silently dropped authority.
        """
        if str(body.get("contract", "")) != STRATEGY_AUTHORIZATION_CONTRACT_V2:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant declares contract {body.get('contract')!r} and this "
                f"build reads {STRATEGY_AUTHORIZATION_CONTRACT_V2}. A grant of "
                "another shape is refused rather than parsed hopefully",
            )
        try:
            return cls(
                authorization_id=str(body["authorization_id"]),
                strategy_id=str(body["strategy_id"]),
                strategy_version=str(body["strategy_version"]),
                strategy_fingerprint=str(body["strategy_fingerprint"]),
                deployment_universe_version=str(body["deployment_universe_version"]),
                deployment_universe_fingerprint=str(body["deployment_universe_fingerprint"]),
                code_commit=str(body["code_commit"]),
                created_at=str(body["created_at"]),
                environment=str(body["environment"]),
                authorization_state=str(body["authorization_state"]),
                approved_by=str(body.get("approved_by", "")),
                notes=str(body.get("notes", "")),
                supersedes=str(body.get("supersedes", "")),
            )
        except KeyError as exc:
            raise StrategyError(
                Code.STRATEGY_NOT_AUTHORIZED,
                f"this grant omits {exc.args[0]!r}, which this contract requires. "
                "A missing authority field is not an empty one",
            ) from exc


def require_authorization(
    authorization: Any,
    *,
    strategy_id: str,
    strategy_version: str,
    fingerprint: str,
    environment: str,
    universe_fingerprint: str = "",
) -> None:
    """Check a grant of either contract, passing it everything it binds.

    The one place that knows both schemas, so production asks a single question
    and neither caller nor contract has to branch. A V2 grant is always asked
    about the universe; a V1 grant is asked the four things it binds, because
    handing it a universe it has no field for would be a silent no-op dressed
    up as a check.

    Args:
        authorization: A `StrategyDeploymentAuthorization` or
            `StrategyDeploymentAuthorizationV2`.
        strategy_id: The strategy asking.
        strategy_version: Its version.
        fingerprint: Its policy digest.
        environment: Where it wants to run.
        universe_fingerprint: The deployment universe it would rank over.
            Required in practice for a V2 grant: empty is refused rather than
            skipped, because a caller that forgot it would otherwise get V1's
            weaker authority from a V2 grant.

    Raises:
        StrategyError: As the grant's own `require` does, plus
            `STRATEGY_NOT_AUTHORIZED` when a universe-binding grant is checked
            without a universe.
    """
    if not isinstance(authorization, StrategyDeploymentAuthorizationV2):
        authorization.require(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            fingerprint=fingerprint,
            environment=environment,
        )
        return
    if not universe_fingerprint.strip():
        raise StrategyError(
            Code.STRATEGY_NOT_AUTHORIZED,
            f"{STRATEGY_AUTHORIZATION_CONTRACT_V2} binds a deployment universe and "
            "none was supplied to check against. The check is refused rather than "
            "narrowed to the strategy alone: silently falling back to the weaker "
            "question is how an unreviewed universe would come to be deployed",
        )
    authorization.require(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        fingerprint=fingerprint,
        environment=environment,
        universe_fingerprint=universe_fingerprint,
    )
