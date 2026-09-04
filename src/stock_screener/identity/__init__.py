"""Security identity: which company a symbol named, and when.

Phase 9A. Everything upstream of this package bounds what a past decision could
*know*; this bounds what it could be *about*. A study that joins on a ticker is
studying a lease rather than a company — `BBBY` names Bed Bath & Beyond until
2023 and an unrelated issuer from 2025 — and no amount of point-in-time care
downstream repairs a security that is two securities.

The layer has three parts, in the order they run:

`ftd`
    Reads the SEC's published fails-to-deliver history — 433 archives, 2004
    onward — into `ftd_identity_observations`: one row per distinct
    `(cusip, symbol, issuer_name)` triple, with the settlement window it was seen
    over. Evidence only. It concludes nothing.

`resolution`
    Decides what the evidence means for one delisting at a time — a symbol *with*
    its date — and writes the securities and their dated identifier intervals.
    Where the evidence fits two issuers, or none, it records that instead of
    choosing.

`contamination`
    Finds live `companies` rows whose ticker the evidence shows two issuers
    holding. Reports; never repairs. Splitting such a row means re-attributing
    every price, filing and score under it, and that is a data operation to be
    authorised on its own.

`audit` assembles the figures, every one of them a query against the tables
rather than a total carried by whichever pass wrote last.

Two boundaries hold the whole design up, and neither is enforced by convention
alone:

- **Nothing here writes to `companies`.** A delisted security must never enter
  the investable set, and current-company identity and historical-security
  identity stay separate spaces with no join between them.
- **Absence of evidence is never evidence of absence.** A security appears in the
  fails file only when a delivery in it failed, and before 2008-09-16 only above
  a reporting threshold. `domain.EvidenceVerdict` has no `ABSENT` member, so
  there is no way to record the opposite conclusion even by accident.
"""

from stock_screener.identity.audit import IdentityAudit, build_identity_audit
from stock_screener.identity.contamination import (
    HANDOVER_GAP_DAYS,
    ContaminatedCompany,
    ContaminationReport,
    Handover,
    detect_contaminated_companies,
)
from stock_screener.identity.ftd import (
    FtdFileResult,
    FtdIngestReport,
    evidence_hash,
    ingest_ftd_file,
    ingest_ftd_history,
)
from stock_screener.identity.resolution import (
    FTD_SOURCE,
    SETTLEMENT_GRACE_DAYS,
    ResolutionOutcome,
    ResolutionReport,
    SymbolResolution,
    as_evidence,
    assess_delisted_symbols,
    assess_delisting,
    attach_identities,
    attach_identity,
    holder_spans,
    resolve_delisted_symbols,
    resolve_delisting,
)

__all__ = [
    "FTD_SOURCE",
    "HANDOVER_GAP_DAYS",
    "SETTLEMENT_GRACE_DAYS",
    "ContaminatedCompany",
    "ContaminationReport",
    "FtdFileResult",
    "FtdIngestReport",
    "Handover",
    "IdentityAudit",
    "ResolutionOutcome",
    "ResolutionReport",
    "SymbolResolution",
    "as_evidence",
    "assess_delisted_symbols",
    "assess_delisting",
    "attach_identities",
    "attach_identity",
    "build_identity_audit",
    "detect_contaminated_companies",
    "evidence_hash",
    "holder_spans",
    "ingest_ftd_file",
    "ingest_ftd_history",
    "resolve_delisted_symbols",
    "resolve_delisting",
]
