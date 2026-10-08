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
    "StrategyDeploymentAuthorization",
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
