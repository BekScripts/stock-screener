"""Binding R1.1 to the R1.0 record, and refusing to read anything else.

R1.0's outputs are inputs here, and inputs to an interpretation phase have to
be immutable or the interpretation means nothing. So this module does not trust
the files on disk: it re-derives each artifact's semantic digest exactly as
R1.0 derived it and compares against `r1_0_fingerprints.json`, then checks that
the completion marker itself hashes as expected.

Two digest shapes exist in the R1.0 record and both are reproduced here:

- a **document** written by `write_document` is serialised with sorted keys and
  a two-space indent, so its fingerprint is the digest of the file's own bytes;
- a **ledger** written as JSON lines has the fingerprint of
  `{"rows": [...]}` over the parsed rows, which is not the file digest — so the
  rows are parsed and re-digested.

Nothing here can repair a mismatch. A mismatch means the record moved, and an
interpretation of a record that moved is not an interpretation of R1.0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stock_screener.backtest_harness.registry import digest_document

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DOCUMENT_FINGERPRINTS",
    "LEDGER_FINGERPRINTS",
    "R1_0_DIR",
    "R1Binding",
    "ReviewBindingError",
    "bind_r1_0",
    "cross_section_fingerprint",
    "file_sha256",
    "ledger_fingerprint",
    "read_jsonl",
]

#: Where R1.0 wrote its record, beneath the control root.
R1_0_DIR = "research/r1_0"

#: `{fingerprint name: file}` for artifacts written by `write_document`, whose
#: fingerprint is the digest of the file itself.
DOCUMENT_FINGERPRINTS: dict[str, str] = {
    "research_split_v1": "research_split_v1.json",
    "baseline_registry": "baseline_registry.json",
    "r1_0_preregistration": "r1_0_preregistration.json",
    "monthly_coverage": "monthly_coverage.json",
    "signal_diagnostics": "signal_diagnostics_compounder_v1.json",
    "spot_audit": "spot_audit.json",
    "bias_audit": "bias_audit.json",
    "r1_0_summary": "r1_0_summary.json",
}

#: `{fingerprint name: file}` for JSON-lines ledgers, whose fingerprint is the
#: digest of the parsed rows rather than of the bytes.
LEDGER_FINGERPRINTS: dict[str, str] = {
    "compounder_deciles": "compounder_deciles.jsonl",
    "COMPOUNDER_TOP_DECILE_V1.trades": "run_COMPOUNDER_TOP_DECILE_V1/trades.jsonl",
    "COMPOUNDER_TOP_DECILE_V1.holdings": "run_COMPOUNDER_TOP_DECILE_V1/holdings.jsonl",
    "COMPOUNDER_TOP_DECILE_V1.nav": "run_COMPOUNDER_TOP_DECILE_V1/nav.jsonl",
    "COMPOUNDER_TOP_DECILE_V1.selection": "run_COMPOUNDER_TOP_DECILE_V1/selection.jsonl",
    "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1.nav": "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/nav.jsonl",
    "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1.trades": (
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/trades.jsonl"
    ),
    "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1.holdings": (
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/holdings.jsonl"
    ),
    "BASELINE_PASSIVE_V1.nav": "run_BASELINE_PASSIVE_V1/nav.jsonl",
}


class ReviewBindingError(RuntimeError):
    """The R1.0 record is absent, or no longer hashes as R1.0 recorded it."""


@dataclass(frozen=True)
class R1Binding:
    """The proven R1.0 record R1.1 reads.

    Attributes:
        directory: Where R1.0 wrote its outputs.
        marker: The R1.0 completion marker's content.
        marker_sha256: The digest of the marker file.
        fingerprints: The fingerprints R1.0 recorded.
        verified: The fingerprint names this phase re-derived and matched.
        dataset_id: The dataset R1.0 bound.
        contract_version: The contract R1.0 honoured.
        split_sha256: The frozen split's digest.
        preregistration_sha256: The preregistration's digest.
    """

    directory: Path
    marker: dict[str, Any]
    marker_sha256: str
    fingerprints: dict[str, str]
    verified: tuple[str, ...]
    dataset_id: str
    contract_version: str
    split_sha256: str
    preregistration_sha256: str
    contract_bindings: dict[str, str] = field(default_factory=dict)

    def path(self, relative: str) -> Path:
        """A file inside the R1.0 record."""
        return self.directory / relative


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON-lines ledger into a list of mappings, in file order.

    Args:
        path: The ledger.

    Returns:
        The rows.

    Raises:
        ReviewBindingError: If the file is absent.
    """
    if not path.is_file():
        raise ReviewBindingError(f"{path} is absent; the R1.0 record is incomplete")
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def ledger_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Re-derive a ledger's fingerprint exactly as R1.0 derived it.

    Args:
        rows: The parsed rows, in file order.

    Returns:
        The digest.
    """
    return digest_document({"rows": [dict(sorted(row.items())) for row in rows]})


def cross_section_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Re-derive the cross-section ledger's fingerprint from its projection.

    Args:
        rows: The parsed cross-section rows, in file order.

    Returns:
        The digest R1.0 recorded under `cross_sections`.
    """
    return digest_document(
        {
            "rows": [
                {
                    "kind": row["kind"],
                    "identity_id": row["identity_id"],
                    "decision": row["decision"],
                    "stage": row["stage"],
                    "score": row["final_score"],
                    "tradability": row["tradability"],
                    "exclusion": row["exclusion"],
                }
                for row in rows
            ]
        }
    )


def bind_r1_0(
    directory: Path,
    *,
    expect_marker_sha: str,
    expect_split_sha: str,
    expect_preregistration_sha: str,
    cross_sections: list[dict[str, Any]] | None = None,
) -> R1Binding:
    """Prove the R1.0 record, or refuse to interpret it.

    Args:
        directory: Where R1.0 wrote its outputs.
        expect_marker_sha: The digest the completion marker must have.
        expect_split_sha: The frozen split's digest.
        expect_preregistration_sha: The preregistration's digest.
        cross_sections: The parsed cross-section rows, when the caller has
            already read them. Read here when not supplied.

    Returns:
        The proven binding.

    Raises:
        ReviewBindingError: If any artifact is absent or no longer hashes as
            R1.0 recorded it.
    """
    marker_path = directory / "r1_0_complete.json"
    fingerprints_path = directory / "r1_0_fingerprints.json"
    for path in (marker_path, fingerprints_path):
        if not path.is_file():
            raise ReviewBindingError(f"{path} is absent; there is no R1.0 record to interpret")
    marker_sha = file_sha256(marker_path)
    if marker_sha != expect_marker_sha.lower():
        raise ReviewBindingError(
            f"the R1.0 completion marker hashes to {marker_sha[:16]}…, "
            f"not {expect_marker_sha[:16]}…; refusing to interpret it"
        )
    marker: dict[str, Any] = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("stage") != "R1_0_COMPLETE":
        raise ReviewBindingError("the marker does not mark a complete R1.0 run")
    recorded = {
        str(name): str(value)
        for name, value in (
            json.loads(fingerprints_path.read_text(encoding="utf-8")).get("fingerprints") or {}
        ).items()
    }
    if recorded != {str(k): str(v) for k, v in (marker.get("result_fingerprints") or {}).items()}:
        raise ReviewBindingError("the fingerprint file and the marker disagree")

    verified: list[str] = []
    for name, relative in DOCUMENT_FINGERPRINTS.items():
        expected = recorded.get(name)
        if expected is None:
            raise ReviewBindingError(f"R1.0 recorded no {name} fingerprint")
        actual = file_sha256(directory / relative)
        if actual != expected:
            raise ReviewBindingError(
                f"{relative} hashes to {actual[:16]}…, not the recorded {expected[:16]}…"
            )
        verified.append(name)
    for name, relative in LEDGER_FINGERPRINTS.items():
        expected = recorded.get(name)
        if expected is None:
            raise ReviewBindingError(f"R1.0 recorded no {name} fingerprint")
        actual = ledger_fingerprint(read_jsonl(directory / relative))
        if actual != expected:
            raise ReviewBindingError(
                f"{relative} re-derives to {actual[:16]}…, not the recorded {expected[:16]}…"
            )
        verified.append(name)
    rows = (
        cross_sections
        if cross_sections is not None
        else read_jsonl(directory / "cross_sections.jsonl")
    )
    actual = cross_section_fingerprint(rows)
    if actual != recorded.get("cross_sections"):
        raise ReviewBindingError(
            f"the cross-section ledger re-derives to {actual[:16]}…, not the recorded value"
        )
    verified.append("cross_sections")

    split_sha = file_sha256(directory / "research_split_v1.json")
    prereg_sha = file_sha256(directory / "r1_0_preregistration.json")
    for label, actual_sha, expected_sha in (
        ("split", split_sha, expect_split_sha),
        ("preregistration", prereg_sha, expect_preregistration_sha),
    ):
        if not actual_sha.startswith(expected_sha.lower().rstrip("…")):
            raise ReviewBindingError(
                f"the {label} hashes to {actual_sha[:16]}…, not {expected_sha[:16]}…"
            )
    return R1Binding(
        directory=directory,
        marker=marker,
        marker_sha256=marker_sha,
        fingerprints=recorded,
        verified=tuple(sorted(verified)),
        dataset_id=str(marker.get("dataset_id")),
        contract_version=str(marker.get("contract_version")),
        split_sha256=split_sha,
        preregistration_sha256=prereg_sha,
        contract_bindings={
            str(k): str(v) for k, v in (marker.get("contract_bindings") or {}).items()
        },
    )
