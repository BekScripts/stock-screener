"""`PAPER_RUNTIME_STRATEGY_RESOLVER_V1` — the binding decides which strategy runs.

P2.3.1H found that production constructed `CompounderMonthlyStrategy` in eleven
places and compared the result against whatever the runtime binding happened to
say. That arrangement has exactly one safe outcome and one unsafe one: a binding
naming V2 is refused (safe, and the runtime is unusable), or some path forgets to
compare and V1 decides under V2's label (a false record of which policy chose the
portfolio). Neither is a deployment.

So the authority is inverted here. **The binding names a strategy and this module
constructs it.** No production path may pick an implementation of its own.

Three refusals carry the design:

- **Unknown is unknown.** An id, version or fingerprint this build does not
  recognise fails closed. There is no "latest" strategy, no nearest match and no
  fallback to V1 — a runtime that silently ran an older policy than its binding
  names would produce decisions nobody authorised.
- **The fingerprint is checked, not trusted.** A registered version whose
  installed code digests differently from the binding's record is refused. That
  is what catches a strategy edited after the grant was issued.
- **A deployment requirement is a requirement.** V2 cannot be built without the
  exact deployment universe its binding names. There is no default universe and
  no empty-universe fallback: an empty universe selects nothing, which looks like
  a quiet no-op rather than the misconfiguration it is.

What this module never does: reach a network, rebuild a universe from provider
data, choose between two registered versions, or construct anything the binding
did not name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from .universe import DeploymentUniverse

__all__ = [
    "DEPLOYABLE_STRATEGIES",
    "STRATEGY_RESOLVER_VERSION",
    "StrategySpec",
    "deployable_strategy_versions",
    "resolve_strategy",
    "strategy_spec",
]

#: The resolver contract.
STRATEGY_RESOLVER_VERSION = "PAPER_RUNTIME_STRATEGY_RESOLVER_V1"


@dataclass(frozen=True)
class StrategySpec:
    """How to construct one runtime-deployable strategy version.

    Attributes:
        strategy_id: The strategy identity the binding must name.
        strategy_version: Its frozen version.
        requires_deployment_universe: Whether construction needs a frozen
            deployment universe. Declared as data so the coverage guard can
            check every registered version against what production supplies,
            rather than discovering the requirement when a run fails.
        factory_name: The class this version constructs, by name, for the
            readiness report and for a refusal message.
    """

    strategy_id: str
    strategy_version: str
    requires_deployment_universe: bool
    factory_name: str

    @property
    def key(self) -> tuple[str, str]:
        """The registry key."""
        return (self.strategy_id, self.strategy_version)

    def build(self, *, universe: DeploymentUniverse | None) -> Any:
        """Construct the strategy, or refuse.

        Args:
            universe: The frozen deployment universe, where this version needs
                one.

        Returns:
            The strategy implementation.

        Raises:
            RuntimeBindingError: `RUNTIME_DEPLOYMENT_UNIVERSE_NOT_AUTHORED` when
                a version that requires a universe is given none.
        """
        from stock_screener.strategies import (
            CompounderMonthlyFractionableStrategy,
            CompounderMonthlyStrategy,
        )

        if not self.requires_deployment_universe:
            return CompounderMonthlyStrategy()
        if universe is None:
            raise RuntimeBindingError(
                Code.RUNTIME_DEPLOYMENT_UNIVERSE_NOT_AUTHORED,
                f"{self.strategy_version} deploys only across a frozen "
                "deployment universe and none was supplied. It is not built with "
                "an empty universe and not built with a universe rebuilt from the "
                "provider: an empty one selects nothing, which reads as a quiet "
                "no-op rather than the misconfiguration it is, and a rebuilt one "
                "would make the strategy's reach depend on when it ran",
            )
        return CompounderMonthlyFractionableStrategy(universe)


#: Every strategy version a runtime binding may name, and what it needs. The one
#: registry: a version absent here cannot be deployed, which is the point.
DEPLOYABLE_STRATEGIES: dict[tuple[str, str], StrategySpec] = {
    spec.key: spec
    for spec in (
        StrategySpec(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version="ALPHATRADER_COMPOUNDER_MONTHLY_V1",
            requires_deployment_universe=False,
            factory_name="CompounderMonthlyStrategy",
        ),
        StrategySpec(
            strategy_id="ALPHATRADER_COMPOUNDER_MONTHLY",
            strategy_version="ALPHATRADER_COMPOUNDER_MONTHLY_V2",
            requires_deployment_universe=True,
            factory_name="CompounderMonthlyFractionableStrategy",
        ),
    )
}


def deployable_strategy_versions() -> tuple[str, ...]:
    """Every strategy version this build can construct, sorted."""
    return tuple(sorted(version for _, version in DEPLOYABLE_STRATEGIES))


def strategy_spec(strategy_id: str, strategy_version: str) -> StrategySpec:
    """The spec for one version, or refuse.

    Args:
        strategy_id: The strategy identity.
        strategy_version: Its version.

    Returns:
        The spec.

    Raises:
        RuntimeBindingError: `RUNTIME_STRATEGY_NOT_RESOLVABLE` for anything not
            registered.
    """
    spec = DEPLOYABLE_STRATEGIES.get((strategy_id, strategy_version))
    if spec is None:
        raise RuntimeBindingError(
            Code.RUNTIME_STRATEGY_NOT_RESOLVABLE,
            f"no production strategy is registered for {strategy_id}/"
            f"{strategy_version}. This build can construct "
            f"{list(deployable_strategy_versions())}. There is no latest "
            "strategy and no fallback to an earlier version: a runtime that ran "
            "a policy other than the one its binding names would record "
            "decisions nobody authorised",
        )
    return spec


def resolve_strategy(
    *,
    strategy_id: str,
    strategy_version: str,
    strategy_fingerprint: str,
    universe: DeploymentUniverse | None = None,
) -> Any:
    """Construct the strategy a runtime binding names.

    The single production entry point. Every decision, execution, status and
    readiness path goes through here, so the binding is the only thing that
    decides which policy a runtime runs.

    Args:
        strategy_id: The identity the binding records.
        strategy_version: The version it records.
        strategy_fingerprint: The policy digest it records. Verified against the
            installed code, never assumed to match.
        universe: The frozen deployment universe, for a version that needs one.

    Returns:
        The strategy implementation, verified to digest to
        `strategy_fingerprint`.

    Raises:
        RuntimeBindingError: `RUNTIME_STRATEGY_NOT_RESOLVABLE` for an
            unregistered id or version, or when the installed code's fingerprint
            differs from the binding's;
            `RUNTIME_DEPLOYMENT_UNIVERSE_NOT_AUTHORED` when a deployment
            universe is required and absent.
    """
    spec = strategy_spec(strategy_id, strategy_version)
    strategy = spec.build(universe=universe)
    installed = strategy.fingerprint()
    if installed != strategy_fingerprint:
        raise RuntimeBindingError(
            Code.RUNTIME_STRATEGY_NOT_RESOLVABLE,
            f"the runtime is bound to {strategy_version} at policy "
            f"{strategy_fingerprint[:16]}… and the installed {spec.factory_name} "
            f"digests to {installed[:16]}…. The code has changed since the "
            "deployment was granted, so the grant no longer covers it. The "
            "installed build is not substituted for the bound one",
        )
    if strategy.strategy_id != strategy_id or strategy.strategy_version != strategy_version:
        raise RuntimeBindingError(
            Code.RUNTIME_STRATEGY_NOT_RESOLVABLE,
            f"the registry maps {strategy_id}/{strategy_version} to a strategy "
            f"identifying itself as {strategy.strategy_id}/"
            f"{strategy.strategy_version}. The registry is wrong and nothing "
            "resolves until it is corrected",
        )
    return strategy
