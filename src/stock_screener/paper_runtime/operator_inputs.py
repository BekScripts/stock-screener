"""The two inputs an operator must author before operations can trade.

Wiring `run-due` to the submission authority exposed something the old dead
path hid: two of the authority's inputs do not exist anywhere, and neither can
be derived from code or data.

**The risk policy.** `RuntimeSubmissionAuthority` needs a `RiskPolicy` — gross
exposure, position weight, position count, cash floor. The repository contains
none that is authorised: `EXAMPLE_POLICY_V1` is marked
`production_authorized=False` and its docstring says promoting those numbers
"requires a dedicated risk-policy phase with its own evidence". The operational
policy the V2 soak protocol bound is an *identity* — `ALPHATRADER_PAPER` / `V1`
— with no limits at all and a field recording
`operator_authored_policy_store: NOT_IMPLEMENTED`.

**The symbol mapping.** The adapter routes a `SecurityIdentity` to a venue
symbol through a declared mapping. The production path builds
`SymbolMapping.of(())`, so nothing is routable; the only mapping anywhere comes
from the canary's own `--identity-id`/`--symbol` arguments.

So this module reads both from artefacts under the operations root and
**refuses when they are absent**. It ships no defaults. A default risk limit is
an unauthorised risk limit wearing a plausible number, and a default symbol
mapping decides which securities the system may buy — neither is a decision a
composition root is entitled to make.

The consequence is deliberate: `run-due` now reaches the submission authority
and then stops, naming the artefact an operator has to author. That is a
different and much better failure than the one it replaces, where the authority
was simply never constructed and the run died on `RUNTIME_BINDING_INVALID` with
nothing to act on.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "OPERATOR_INPUTS_VERSION",
    "RISK_POLICY_FILENAME",
    "SYMBOL_MAPPING_FILENAME",
    "OperatorInputStatus",
    "operator_input_status",
    "read_risk_policy",
    "read_symbol_mapping",
    "stored_marks",
    "write_risk_policy",
    "write_symbol_mapping",
]

#: The operator-input contract.
OPERATOR_INPUTS_VERSION = "OPERATOR_AUTHORED_RUNTIME_INPUTS_V1"

#: Where each artefact lives, under `<root>/runtime/`.
RISK_POLICY_FILENAME = "risk_policy.json"
SYMBOL_MAPPING_FILENAME = "symbol_mapping.json"


@dataclass(frozen=True)
class OperatorInputStatus:
    """Which operator inputs are present, and what is missing.

    Attributes:
        risk_policy_present: Whether an authored risk policy exists.
        symbol_mapping_present: Whether an authored symbol mapping exists.
        missing: The artefacts an operator still has to author.
        detail: Why, for a reader.
    """

    risk_policy_present: bool
    symbol_mapping_present: bool
    missing: tuple[str, ...]
    detail: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether operations have everything they need to size an order."""
        return not self.missing

    def as_json(self) -> dict[str, Any]:
        """Return the status as a JSON-safe mapping."""
        return {
            "version": OPERATOR_INPUTS_VERSION,
            "risk_policy_present": self.risk_policy_present,
            "symbol_mapping_present": self.symbol_mapping_present,
            "missing": list(self.missing),
            "complete": self.complete,
            "detail": list(self.detail),
        }


def _directory(root: Path) -> Path:
    """The runtime directory the artefacts live in."""
    from .binding import RUNTIME_DIRECTORY

    return root / RUNTIME_DIRECTORY


def operator_input_status(root: Path) -> OperatorInputStatus:
    """Report which operator inputs exist, without reading a network.

    Args:
        root: The operations root.

    Returns:
        The status.
    """
    directory = _directory(root)
    policy = (directory / RISK_POLICY_FILENAME).exists()
    mapping = (directory / SYMBOL_MAPPING_FILENAME).exists()
    missing: list[str] = []
    detail: list[str] = []
    if not policy:
        missing.append("RISK_POLICY")
        detail.append(
            f"no authored risk policy at runtime/{RISK_POLICY_FILENAME}; no risk "
            "limits exist in this repository that are authorised for use, and a "
            "default would be an unauthorised limit wearing a plausible number"
        )
    if not mapping:
        missing.append("SYMBOL_MAPPING")
        detail.append(
            f"no authored symbol mapping at runtime/{SYMBOL_MAPPING_FILENAME}; "
            "nothing declares which securities this runtime may route to the venue"
        )
    return OperatorInputStatus(
        risk_policy_present=policy,
        symbol_mapping_present=mapping,
        missing=tuple(missing),
        detail=tuple(detail),
    )


def _read_body(path: Path, *, code: str, what: str) -> dict[str, Any]:
    """Read one authored artefact, refusing rather than defaulting."""
    if not path.exists():
        raise RuntimeBindingError(
            code,
            f"{what} has not been authored at {path}. Operations do not invent one: "
            "a composition root that supplies its own would be deciding something an "
            "operator has to decide, and the number would look authoritative",
        )
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"{what} at {path} could not be read: {type(exc).__name__}",
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"{what} at {path} is not an object",
        )
    return body


def read_risk_policy(root: Path) -> Any:
    """Read the authored risk policy for this runtime.

    Args:
        root: The operations root.

    Returns:
        The `RiskPolicy`, exactly as authored. No limit is relaxed, defaulted or
        widened here.

    Raises:
        RuntimeBindingError: `RUNTIME_RISK_POLICY_NOT_AUTHORED` when the
            artefact is absent, or `RUNTIME_OPERATOR_INPUT_UNREADABLE` when it
            cannot be parsed or omits a required limit.
    """
    from stock_screener.portfolio_risk import RiskPolicy

    path = _directory(root) / RISK_POLICY_FILENAME
    body = _read_body(
        path, code=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED, what="the runtime risk policy"
    )
    required = (
        "policy_id",
        "policy_version",
        "max_gross_exposure",
        "max_position_weight",
        "max_positions",
        "min_cash_weight",
    )
    absent = [name for name in required if name not in body]
    if absent:
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"the authored risk policy at {path} omits {absent}. A missing limit is "
            "not an absent limit: it would be filled by a default nobody approved",
        )
    try:
        return RiskPolicy(
            policy_id=str(body["policy_id"]),
            policy_version=str(body["policy_version"]),
            long_only=bool(body.get("long_only", True)),
            max_gross_exposure=float(body["max_gross_exposure"]),
            max_net_exposure=(
                None if body.get("max_net_exposure") is None else float(body["max_net_exposure"])
            ),
            max_position_weight=float(body["max_position_weight"]),
            max_positions=int(body["max_positions"]),
            min_cash_weight=float(body["min_cash_weight"]),
            production_authorized=bool(body.get("production_authorized", False)),
        )
    except Exception as exc:
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"the authored risk policy at {path} is not a valid policy: "
            f"{type(exc).__name__}: {exc}",
        ) from exc


def read_symbol_mapping(root: Path) -> Any:
    """Read the authored venue symbol mapping for this runtime.

    Args:
        root: The operations root.

    Returns:
        The `SymbolMapping`.

    Raises:
        RuntimeBindingError: `RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED` when absent,
            or `RUNTIME_OPERATOR_INPUT_UNREADABLE` when malformed.
    """
    from stock_screener.execution.external import SymbolMapping, VenueSymbol

    path = _directory(root) / SYMBOL_MAPPING_FILENAME
    body = _read_body(
        path,
        code=Code.RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED,
        what="the runtime venue symbol mapping",
    )
    rows = body.get("symbols")
    if not isinstance(rows, list) or not rows:
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"the authored symbol mapping at {path} declares no symbols. An empty "
            "mapping routes nothing, and a run that cannot route cannot size",
        )
    try:
        return SymbolMapping.of(
            VenueSymbol(
                kind=str(row.get("kind", "COMPANY")),
                identity_id=int(row["identity_id"]),
                symbol=str(row["symbol"]),
                # Declared per symbol, as the venue reports it. The default is
                # whole shares only: an undeclared instrument is not assumed
                # fractionable, because a rounded quantity is not the
                # authorised order.
                fractionable=bool(row.get("fractionable", False)),
            )
            for row in rows
        )
    except Exception as exc:
        raise RuntimeBindingError(
            Code.RUNTIME_OPERATOR_INPUT_UNREADABLE,
            f"the authored symbol mapping at {path} is not valid: {type(exc).__name__}: {exc}",
        ) from exc


def stored_marks(*, settings: Any, mapping: Any, as_of: str = "") -> dict[str, Decimal]:
    """Sizing marks for every identity this runtime can route.

    Read from the stored price history rather than fetched at submission time,
    so a replay sizes against the same numbers. An identity with no stored
    close gets no mark, and the submission authority already refuses to size an
    identity it has no mark for rather than sending one at a guessed price.

    Args:
        settings: Typed application settings, for the database.
        mapping: The authored symbol mapping, naming the routable identities.
        as_of: Size against the last close on or before this date, ISO. Empty
            means the latest stored close.

    Returns:
        Marks by identity label. Identities with no stored price are absent.
    """
    from datetime import date

    from data_access import PriceHistoryRepository
    from data_access.session import session_scope

    boundary = date.fromisoformat(as_of) if as_of else None
    marks: dict[str, Decimal] = {}
    with _session_factory(settings) as factory, session_scope(factory) as session:
        prices = PriceHistoryRepository(session)
        for entry in mapping.entries:
            bars = prices.list_for_company(entry.identity_id, until=boundary)
            if not bars:
                continue
            marks[f"{entry.kind}:{entry.identity_id}"] = Decimal(str(bars[-1].close))
    return marks


@contextmanager
def _session_factory(settings: Any) -> Iterator[Any]:
    """Open a database session factory for the configured database."""
    from data_access import build_session_factory, create_engine_from_url

    engine = create_engine_from_url(settings.database_url)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def write_risk_policy(root: Path, body: dict[str, Any]) -> Path:
    """Persist an authored risk policy.

    The operator action, exposed so a CLI command or an offline test root can
    perform it explicitly. It writes exactly what it is given: this function
    authorises nothing and supplies no limit of its own.

    Args:
        root: The operations root.
        body: The authored policy.

    Returns:
        Where it was written.
    """
    from stock_screener.paper_operations import atomic_write_verified

    path = _directory(root) / RISK_POLICY_FILENAME
    atomic_write_verified(path, {"version": OPERATOR_INPUTS_VERSION, **body})
    return path


def write_symbol_mapping(root: Path, symbols: list[dict[str, Any]]) -> Path:
    """Persist an authored venue symbol mapping.

    Args:
        root: The operations root.
        symbols: The declared symbols.

    Returns:
        Where it was written.
    """
    from stock_screener.paper_operations import atomic_write_verified

    path = _directory(root) / SYMBOL_MAPPING_FILENAME
    atomic_write_verified(path, {"version": OPERATOR_INPUTS_VERSION, "symbols": list(symbols)})
    return path
