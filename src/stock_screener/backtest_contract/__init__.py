"""The backtest data contract: what historical research may see, stated once and bound.

The evidence programme is closed. `SEC_EVIDENCE_GENERATION_V2` is frozen at
99,774 trusted artifacts; the point-in-time materialisation over it is the
B2.16 package; the residual is audited and accepted. What is left to do
before strategy research is to say — in one machine-readable place — which
data a historical decision may use, when it became knowable, which identity
it belongs to, what the labels are and how they are kept apart from the
features, and what the dataset's digests are, so that a backtest either
binds to exactly this or refuses to run.

Three names, three concepts:

    SEC_EVIDENCE_GENERATION_V2   the evidence generation (what was fetched and proven)
    PIT_RESEARCH_DATASET_V2      the frozen research dataset built over it
    BACKTEST_DATA_CONTRACT_V1    the rules a backtest must honour to read it

Nothing here changes a rule. Every semantic the contract states is one the
repository already enforces somewhere — `stock_screener.pit.decision_time`,
`domain.outcomes`, `domain.membership`, the identity layer, the PIT
assembly — and the contract's job is to name the rule, the module that owns
it, and its limits, then to fingerprint the tables a research row is built
from so the binding is checkable.
"""

from stock_screener.backtest_contract.contract import (
    CONTRACT_DIR,
    CONTRACT_FILES,
    CONTRACT_MIRROR_PREFIX,
    CONTRACT_VERSION,
    DATASET_ID,
    ContractBindings,
    ContractError,
    build_contract,
    fingerprint_dataset,
    load_contract,
    mirror_contract,
    readiness_matrix,
    run_manifest_schema,
    write_contract,
)

__all__ = [
    "CONTRACT_DIR",
    "CONTRACT_FILES",
    "CONTRACT_MIRROR_PREFIX",
    "CONTRACT_VERSION",
    "DATASET_ID",
    "ContractBindings",
    "ContractError",
    "build_contract",
    "fingerprint_dataset",
    "load_contract",
    "mirror_contract",
    "readiness_matrix",
    "run_manifest_schema",
    "write_contract",
]
