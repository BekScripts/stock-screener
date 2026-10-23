"""What a research run must prove before it is allowed to compute anything.

The dataset is frozen and the contract says so. This module is the gate: it
re-hashes every artifact a backtest reads, loads `BACKTEST_DATA_CONTRACT_V1`
under those digests, and refuses to go further if any of them has moved. There
is deliberately no override — a `--force` here would turn the one mechanism
that makes a historical result citable into a comment.

Two artifacts beyond the contract's own bindings are needed by the harness and
are therefore bound here as well:

- `a3_inputs_r1/pit_coverage_v2.jsonl` — the pre-A3 point-in-time coverage of
  every universe month, carrying the frozen classification verdict and, where
  evidence already existed, the acceptance date that established it.
- `post_a3_pit/pit_month_outcomes.jsonl` — what the A3 expansion resolved, one
  row per month that had been open, with the acceptance date of the filing
  that closed it.

Together they say, for every identity-month, whether the security was
established common stock on evidence that existed by the month's end. That is
the historical research universe, and it is read from frozen files rather than
recomputed, because recomputing it would put the universe under this phase's
control instead of the evidence programme's.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stock_screener.backtest_contract import (
    CONTRACT_VERSION,
    DATASET_ID,
    ContractBindings,
    ContractError,
    load_contract,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "COVERAGE_FILE",
    "OUTCOMES_FILE",
    "BindingError",
    "HarnessBindings",
    "file_sha256",
    "verify_bindings",
]

#: The frozen pre-A3 coverage of every universe month, under the generation root.
COVERAGE_FILE = "a3_inputs_r1/pit_coverage_v2.jsonl"

#: What the A3 expansion resolved, under the generation root.
OUTCOMES_FILE = "post_a3_pit/pit_month_outcomes.jsonl"


class BindingError(RuntimeError):
    """A frozen input is absent, or does not hash to what the run was told."""


@dataclass(frozen=True)
class HarnessBindings:
    """Everything one R1 run is bound to, proven before it computes anything.

    Attributes:
        contract: The loaded contract's content.
        bindings: The contract bindings the run presented and the contract
            accepted.
        coverage_sha256: Digest of the frozen pre-A3 coverage file.
        outcomes_sha256: Digest of the frozen post-A3 month outcomes file.
        database_path: The research database, opened read-only.
        universe_fingerprint: The frozen `historical_universe` projection digest.
        price_fingerprint: The frozen `price_history` projection digest.
        historical_price_fingerprint: The frozen `historical_prices` digest.
        label_fingerprint: The frozen `forward_return_labels` digest.
        signal_fingerprint: The frozen `signal_observations` digest.
    """

    contract: dict[str, object]
    bindings: ContractBindings
    coverage_sha256: str
    outcomes_sha256: str
    database_path: str
    universe_fingerprint: str
    price_fingerprint: str
    historical_price_fingerprint: str
    label_fingerprint: str
    signal_fingerprint: str

    @property
    def dataset_id(self) -> str:
        """The dataset the run reads."""
        return self.bindings.dataset_id

    @property
    def contract_version(self) -> str:
        """The contract the run honours."""
        return self.bindings.contract_version


def file_sha256(path: Path) -> str:
    """Digest a file in one megabyte blocks.

    Args:
        path: The file.

    Returns:
        Its lowercase hexadecimal SHA-256.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(bindings: ContractBindings, name: str) -> str:
    held = bindings.fingerprints.get(name)
    if not isinstance(held, dict):
        raise BindingError(f"the contract carries no {name} fingerprint; refusing to run")
    sha = held.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise BindingError(f"the {name} fingerprint is not a digest; refusing to run")
    return sha


def verify_bindings(
    contract_dir: Path,
    generation_dir: Path,
    *,
    expect: ContractBindings,
    expect_coverage_sha: str,
    expect_outcomes_sha: str,
    database_path: Path,
) -> HarnessBindings:
    """Prove every frozen input, load the contract under it, or refuse.

    Fail-closed by construction: the contract is loaded through
    `backtest_contract.load_contract`, which compares every binding the caller
    presents against the ones the contract was written under and raises on any
    difference. The two point-in-time files are hashed here because the
    contract does not name them.

    Args:
        contract_dir: Where `BACKTEST_DATA_CONTRACT_V1` was written.
        generation_dir: The evidence generation's plan root, holding the
            coverage and outcome files.
        expect: The bindings the caller has re-proven from its own inputs.
        expect_coverage_sha: Digest the pre-A3 coverage file must have.
        expect_outcomes_sha: Digest the post-A3 outcomes file must have.
        database_path: The research database the run will open read-only.

    Returns:
        The proven bindings, carrying the contract.

    Raises:
        BindingError: If a file is absent or hashes differently, if a required
            fingerprint is missing, or if the contract refuses the bindings.
    """
    if expect.dataset_id != DATASET_ID or expect.contract_version != CONTRACT_VERSION:
        raise BindingError(
            f"a run must present {DATASET_ID}/{CONTRACT_VERSION}, "
            f"not {expect.dataset_id}/{expect.contract_version}"
        )
    digests: dict[str, str] = {}
    for name, expected in (
        (COVERAGE_FILE, expect_coverage_sha),
        (OUTCOMES_FILE, expect_outcomes_sha),
    ):
        path = generation_dir / name
        if not path.is_file():
            raise BindingError(f"{path} is absent; the frozen universe cannot be read")
        actual = file_sha256(path)
        if actual != expected.lower():
            raise BindingError(
                f"{name} hashes to {actual[:16]}…, not {expected[:16]}…; refusing to run"
            )
        digests[name] = actual
    if not database_path.is_file():
        raise BindingError(f"{database_path} is absent; there is no dataset to read")

    try:
        contract = load_contract(contract_dir, expect=expect)
    except (ContractError, OSError, ValueError) as exc:
        raise BindingError(f"the contract refused this run: {exc}") from exc

    return HarnessBindings(
        contract=contract,
        bindings=expect,
        coverage_sha256=digests[COVERAGE_FILE],
        outcomes_sha256=digests[OUTCOMES_FILE],
        database_path=str(database_path),
        universe_fingerprint=_fingerprint(expect, "historical_universe"),
        price_fingerprint=_fingerprint(expect, "price_history"),
        historical_price_fingerprint=_fingerprint(expect, "historical_prices"),
        label_fingerprint=_fingerprint(expect, "forward_return_labels"),
        signal_fingerprint=_fingerprint(expect, "signal_observations"),
    )
