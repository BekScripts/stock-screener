---
title: Storage layout
type: reference
---

# Storage layout

Where this project's data lives, which parts are authoritative, and which parts
must never be written to the boot disk.

## The two disks

| Location | Holds | Why there |
| --- | --- | --- |
| **Local (boot disk)** | `compounder_radar.db`, the authoritative research staging DB, source, migrations, tests | small, queried constantly, must survive without external hardware |
| **External volume** | SEC evidence corpus, FTD archives, future staging databases, compressed archives of superseded databases | hundreds of gigabytes; the boot disk cannot hold it |

The split exists because the SEC evidence corpus is on the order of **359 GB**
at full density and the boot disk has nothing like that free. A harvest that
defaulted to a local path would fill it.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./compounder_radar.db` | the database the application reads and writes |
| `FTD_CACHE_DIR` | `.cache/sec-ftd` | SEC fails-to-deliver archives, ~600 MB over 433 files |
| `SEC_EVIDENCE_DIR` | **none** | root of the SEC evidence corpus — the *artifact plane* |
| `SEC_CONTROL_DIR` | **none** (legacy colocated layout) | root of the evidence *control plane* — manifest, checkpoints, plans, derived index |

`SEC_EVIDENCE_DIR` having no default is the point, not an oversight. So is
`SEC_CONTROL_DIR` having no default of its own: unset means the control plane
sits beneath the artifact root, which is how every corpus before B2.5 was laid
out, and that arrangement is reported as a shared failure domain rather than
silently accepted. See [Control plane](#b25-control-plane) below.

## Why the evidence root fails loudly

`stock_screener.evidence` never creates the root. It reports one of five states
and raises for four of them:

| State | Means | Fix |
| --- | --- | --- |
| `UNCONFIGURED` | the setting is unset | set it |
| `UNAVAILABLE` | the path does not exist | mount the volume |
| `NOT_A_DIRECTORY` | it exists and is a file | fix the path |
| `EMPTY` | a real directory holding nothing | harvest, or check the root |
| `AVAILABLE` | usable | — |

`EMPTY` is deliberately separate from `UNAVAILABLE`. If an unmounted volume were
allowed to become an empty directory, every downstream reader would keep working
and report *no evidence* — indistinguishable from a corpus that genuinely holds
none. That is not a hypothetical: Phase 9K lost a 19 GB corpus to an ephemeral
`/private/tmp` directory, and the loss was noticed only because a size check
happened to run.

Two rules follow, and both are enforced by tests rather than convention:

- **The root is never created.** `test_resolving_a_missing_root_does_not_create_it`
  asserts the parent directory does not appear either.
- **There is no fallback path.** No state resolves to a substitute directory;
  every failing one raises.

## Layout on the external volume

```text
<volume>/stock-screener-data/
├── sec/
│   ├── filings/       raw filing documents
│   ├── submissions/   submissions JSON per CIK
│   ├── fulltext/      EDGAR full-text search responses
│   └── manifests/     URL, HTTP status, SHA-256, bytes, retrieval time
├── ftd/               SEC fails-to-deliver archives
├── staging/           large research databases
└── archives/
    └── superseded/    compressed databases, with MANIFEST.tsv
```

`SEC_EVIDENCE_DIR` points at `<volume>/stock-screener-data/sec`. That is the
**legacy colocated layout**: `manifests/`, `checkpoints/` and `derived/` sit
beside the artifacts on the same volume. Since B2.5 the control plane can — and
for any new corpus should — live elsewhere; see below.

### Paths beneath the evidence root

Introduced in Phase 9K.13, when the evidence writer was built. Nothing in this
repository stored SEC documents on disk before it, so this is a new convention
rather than an inherited one.

```text
filings/<cik padded to 10>/<accession without dashes>/<document name>
submissions/CIK<cik padded to 10>.json
fulltext/<sha256 of the query>.json
manifests/evidence.jsonl
```

The padded CIK and the dashless accession are the SEC's own archive form, so a
stored path reads back to the URL it came from:
`filings/0000886158/000088615823000044/bbby-20230502.htm` is
`https://www.sec.gov/Archives/edgar/data/886158/000088615823000044/bbby-20230502.htm`.
A full-text query is digested instead of spelled out because it carries
quotation marks, slashes and arbitrary length, none of which survive a file
name. A document name containing a path separator or `..` is **refused**, not
sanitised — a quietly renamed file would no longer be the document the manifest
names.

### The manifest is append-only

One JSON object per line, appended and fsynced individually. The full corpus is
expected to reach ~122,000 filings, and rewriting a manifest that size after
every request would cost more than the download. Entries are never edited: a
retry appends a second entry, so the log is a history and `latest_by_url` folds
it into a current state when one is wanted. A reader that meets a truncated
final line — the one shape a crash during append can leave — discards it and
keeps everything before it.

Fields: `url`, `outcome`, `status`, `sha256`, `bytes`, `retrieved_at`, `path`,
`request_kind`, `content_type`, `etag`, `last_modified`, `cik`, `accession`,
`form`, `filing_date`, `error`, `attempt`, `recorded_at`. The first five are
what make an artifact auditable and are always present on a stored one; the rest
are recorded when the server or the caller supplied them and are `null`
otherwise. An absent `etag` means the SEC sent none — never that one was not
looked for.

### What counts as already having an artifact

Not that the file exists. An artifact is reused only when the manifest records
it as stored **and** the bytes on disk still hash to the digest recorded. Any
other state is `INVALID_CACHE` and is fetched again.

A file on disk with **no manifest entry** is treated as absent and re-fetched.
It could be adopted by hashing it, but the digest would then prove nothing:
there would be no record of which URL produced it or when, and provenance
invented after the fact is not provenance. One request buys a file that can be
defended.

## B2.2–B2.4 storage contract checkpoint

`EvidenceStorage` owns six artifact operations: `exists`, `stat`, `read_bytes`,
`write_bytes`, `digest`, and `list_prefix`. `LocalEvidenceStorage` is the only
production implementation. The in-memory test implementation proves that
harvesting, parsing, cache verification and reconciliation need no artifact
filesystem path. No object-store backend, SDK, credentials or migration is
introduced by this checkpoint.

### Artifact and control planes

| Plane | Contents | Access |
| --- | --- | --- |
| Artifact | `filings/`, `submissions/`, `fulltext/` | All runtime artifact operations use `EvidenceStorage` and logical keys. |
| Control | Manifest JSONL, checkpoints, plans, target manifests, derived indexes, fingerprints, migration ledgers | Remain local filesystem data in their existing locations. No relocation in B2.4. |

The **manifest file** is control-plane data. Its **`Entry.path` field** is an
artifact-plane logical key, never a local absolute path. Extraction and audit
refuse a manifest key outside the three artifact prefixes before reading it.
The generic backend contract validates canonical relative keys; the evidence
runtime additionally enforces the artifact prefixes.
Dot-prefixed key segments are reserved for backend internals and rejected by
all backends; a successful artifact write must remain visible in listing.

### Exception contract

| Result or exception | Meaning |
| --- | --- |
| `exists(key) == False` | The backend successfully established that the object is absent. |
| `ArtifactNotFoundError` | That same definite absence, from `read_bytes`, `stat` or `digest`. |
| `StorageUnavailableError` | Storage cannot reliably answer: unavailable root, I/O failure, or a future backend's authentication, network or service failure. |
| `UnsafeStorageKeyError` | Invalid logical key; never normalized, repaired or looked up as a missing object. The local backend also refuses symlink traversal beneath its root. |
| `EvidenceWriteError` | Artifact write did not complete successfully. It is a `StorageUnavailableError`; no successful manifest entry may be appended. |

An existing root with a missing object is different from a missing root. The
latter raises, including on `exists` and `list_prefix`. Local enumeration uses
explicit directory traversal so nested listing failures cannot disappear into
an apparently successful empty listing. A failure after enumeration starts also
aborts the operation. Local AppleDouble sidecars and hidden temporary files are
excluded by name before inspecting their metadata.

**ETag is never evidence SHA-256.** No future object-store backend may substitute
ETag for `StoredArtifact.sha256`, `EvidenceStorage.digest()` or manifest
SHA-256. Digests describe the actual artifact bytes; ETag remains opaque source
metadata. The lifecycle test supplies a deliberately different ETag and verifies
that the manifest and stored bytes retain their actual SHA-256.

### Parser failure policy

| Artifact state | Extraction / submissions expansion |
| --- | --- |
| Readable | Parse the backend bytes normally. |
| Definitely absent | Extraction increments `missing_files`; expansion produces no targets. |
| Unsafe key | Raise `UnsafeStorageKeyError`, an integrity/path violation. |
| Backend unavailable or I/O failure | Propagate the failure; do not count it as missing or malformed. |
| Malformed document | Preserve existing parser behavior: malformed submissions JSON yields no catalogue/targets; tolerant HTML parsing may succeed with no cover rows, while existing parser exceptions remain parse failures. |

This is an **intentional safety correction**, not unchanged behavior. Pre-B2
extraction and expansion broadly swallowed `OSError`. A storage outage could
therefore silently remove PIT evidence while appearing to complete normally.
The storage seam catches only definite absence; it does not change PIT identity,
evidence dates, classification or parser strategy.

### Reconciliation counters

Full and subset reconciliation share the following policy:

| State | Result |
| --- | --- |
| Trusted, matching artifact | No problem counter. |
| Unclaimed listed object | `orphan_files += 1`. The legacy name also denotes non-filesystem objects. |
| Safe trusted key, definitely absent | `orphan_records += 1`. |
| Unsafe trusted manifest key | `path_violations += 1` only; no lookup and no orphan-record increment. |
| Present key inconsistent with its source URL | `path_violations += 1`; byte/digest verification still runs. |
| Byte count differs | `byte_mismatches += 1`, independently of digest verification. |
| Same size, different SHA-256 | `hash_mismatches += 1`, no byte mismatch. |
| Trusted empty object | `zero_byte += 1`; its empty SHA-256 can still match. |
| Listing or other backend failure | Propagate; no completed reconciliation result. |

Size, digest and zero-byte checks are explicitly orthogonal: a truncated object
can increment both size and digest counters. Unsafe-key and missing-object
counters are mutually exclusive. `temporary_files` remains a compatibility
field fixed at zero because backend internals are excluded from listing.
`verify_digests_of` has no path counter and raises on an unsafe key instead of
counting it missing.

Full reconciliation enumerates exactly the three artifact prefixes. Subset
reconciliation enumerates only prefixes touched by the selected batch and
compares listed keys against the **whole trusted manifest set**. A current
artifact and an older trusted sibling therefore remain valid while a genuine
orphan sibling is found. The test asserts the exact batch prefix requested;
there is no full-corpus artifact listing per batch. Manifest replay and a shared
submissions-prefix listing can still grow with the corpus; the optimization
bounds artifact listing by touched prefixes, not every part of the audit.

### Transitional EvidenceRoot responsibilities

| Responsibility | B2.4 decision |
| --- | --- |
| `.storage` and six delegated operations | Keep the local backend adapter so existing callers work. Later composition can inject storage directly via `harvest_artifacts` and the other storage-facing functions. |
| `.manifests` | Keep the existing local control-plane path; separate its location in B2.5. |
| `.checkpoints` responsibility | No such property currently exists; `checkpoint_path(root, ...)` builds the local path. Keep the helper until B2.5 separates control-plane placement. |
| `.derived` responsibility | No such property currently exists; `derived_root(root)` supplies the local path. Keep it until B2.5. |
| `filing_path`, `submissions_path`, `fulltext_path` and directory properties | Keep as local compatibility helpers. Runtime artifact consumers use keys instead; remove helpers later when compatibility callers no longer need them. |
| Root availability, layout initialization and free-space preflight | Keep local bootstrap/orchestration behavior. This checkpoint does not make the entire sweep command remote-ready. |

No broad cleanup or control-plane hardening is included. Local operations are
not a transactional defense against concurrent mount replacement or hostile
filesystem mutation. A vanished root and observed storage failures raise, but
mount identity pinning is not implemented.

### Filesystem search audit

Audit scope: `src/stock_screener/sec_evidence/`, `evidence.py`, and the SEC
evidence commands in `cli.py`. Search terms: `Path(`, `open(`, `read_bytes(`,
`read_text(`, `write_bytes(`, `rglob(`, `iterdir(`, `os.`, `shutil.`; additionally
reviewed path builders, `stat`, `is_file`, `is_dir`, and artifact-key callers.

| Remaining occurrence | Classification |
| --- | --- |
| `backend.py`: path resolution, reads, streamed SHA, metadata, directory enumeration | Artifact backend implementation. |
| `storage.py`: atomic writer and legacy `digest_of`; `EvidenceRoot` delegates | Artifact backend implementation / compatibility, no runtime artifact consumer bypass. |
| `storage.py` root setup and path builders; `evidence.py` root inspection; `sweep.py` disk-space/root preflight; CLI layout status | Local bootstrap and control-plane orchestration; inspect the configured root/volume, never read or parse artifact bytes. |
| `manifest.py` append/read/fsync | Control plane: provenance log file. |
| `sweep.py` checkpoint read/write/fsync | Control plane: checkpoints. |
| `binding.py` paths, reads and hashes | Control plane: source database, derived index and corpus fingerprint. |
| `targets.py` reads; `plan.py` file writes | Control plane: target manifests and plans. |
| `extract.py` index SQLite connection | Control plane: derived index; artifact reads use `storage.read_bytes`. |
| `harvest.py`, `extract.py`, `expand.py` `read_bytes`/`write_bytes` calls | Calls through the artifact contract, not filesystem access. |

**Unjustified artifact leaks: 0.** FTD spreadsheet ingestion and non-evidence
CSV exports elsewhere in `cli.py` are outside the SEC evidence artifact plane.
Control-plane failure handling and relocation remain **B2.5 — RELOCATE AND
HARDEN LOCAL CONTROL PLANE**, not work performed by this checkpoint.

Regression suites: `test_storage_contract.py`, `test_storage_local.py`,
`test_storage_equivalence.py`, `test_storage_neutral.py`, plus the existing
`test_sec_evidence_*` tests.

## B2.5 control plane

The SSD loss in Phase 9L.6 was two losses in one. The 19,350 harvested filings
were the recoverable half — the SEC still serves them. The manifest that said
which URL produced each file, the checkpoints that said which 17 batches were
complete, and the frozen plan that said what remained were the irreplaceable
half, and they sat on the same drive. B2.5 separates them.

### The two planes

| Plane | Holds | Root | Access |
| --- | --- | --- | --- |
| **Artifact** | `filings/`, `submissions/`, `fulltext/` | `SEC_EVIDENCE_DIR` today; an object store later | `EvidenceStorage` and logical keys, nothing else |
| **Control** | `manifests/evidence.jsonl`, `checkpoints/`, `plans/`, `migration/` (reserved), `derived/` | `SEC_CONTROL_DIR` | local filesystem, through `EvidenceControlRoot` |

The manifest is control-plane data whose `path` field is an artifact-plane
key. It records *which* artifact, never *where*: the same manifest line is
written whether the artifact landed on a local directory or, later, in a
bucket, and the control plane never lists, reads or writes an artifact to do
its job. `test_no_control_plane_operation_lists_the_artifact_namespace` holds
that.

### Critical versus rebuildable

Not everything under the control root is equally precious.

| Subtree | Classification | Lose it and… |
| --- | --- | --- |
| `manifests/` | **critical** | the corpus has no provenance; every byte must be refetched to be vouched for |
| `checkpoints/` | **critical** | a sweep cannot prove which batches completed |
| `plans/` | **critical** | the frozen target set (Plan A3 and its remainder) is gone |
| `migration/` | **critical**, reserved | a future artifact migration cannot say what it moved |
| `derived/` — `sec_evidence_index.db`, `corpus-fingerprint.json` | **rebuildable** | run the offline extraction again; nothing is lost |

`sec_evidence_index.db` is a reproducible derived artifact and not source
evidence: the manifest plus the artifact corpus is the source of truth, and the
index is recomputed from them. It stays a local cache under `derived/` and is
never uploaded or mirrored. The fingerprint describes the corpus an index was
built from and is classified with the index.

### Resolving the control root

`SEC_CONTROL_DIR` is inspected the way `SEC_EVIDENCE_DIR` is, and for the same
reason: a control root that could be conjured on the boot disk when the
configured one was unreachable would be a second, empty source of truth.

| State | Means | Result |
| --- | --- | --- |
| `UNCONFIGURED` | unset | the **legacy colocated layout** — control plane beneath the artifact root, reported as a shared failure domain |
| `UNAVAILABLE` | the path does not exist | raises; **never created** |
| `NOT_A_DIRECTORY` | it exists and is a file | raises |
| `AVAILABLE` | a directory | `manifests/` and `checkpoints/` are created beneath it; `plans/`, `migration/` and `derived/` appear when something writes them |

A relative value resolves against the working directory, so `.evidence` (which
`.gitignore` covers) puts the control plane in the project on the boot disk.
The fallback exists so that existing corpora and development setups keep
working unchanged; it is transitional, and `evidence status`, `harvest`,
`harvest-manifest` and `sweep` all print which arrangement is in force. When a
configured control root sits on the same device as the artifact root — a
different directory on the same external drive — the layout prints a warning,
decided from the device number rather than the path.

### Two absences that must not be confused

An unwritten manifest in a present directory is first-time initialisation and
reads as empty. A manifest whose *directory* has gone is the control plane
being unavailable, and reading it as empty would present a corpus of a hundred
thousand vouched artifacts as a corpus of none. `Manifest.entries()` and
`Manifest.append()` raise `ControlPlaneUnavailableError` in the second case and
never create the directory. `load_checkpoint` draws the same line: a missing
file in a present `checkpoints/` is a chunk never attempted; a missing
`checkpoints/` is an unavailable control root, not a sweep that should start
again from batch one.

### What each failure looks like now

| Scenario | Manifest, checkpoints, plans | Artifact operations |
| --- | --- | --- |
| artifact root gone, control root healthy | readable — recovery can be planned from them | `StorageUnavailableError`; no refetch into thin air, no request spent |
| control root gone, artifact root healthy | `ControlPlaneUnavailableError`; nothing created, nothing assumed | untouched |
| legacy colocated root gone | both gone together — the arrangement being retired | both raise |

### Capacity is a local-backend question

The sweep's free-space reserve is a property of a disk. `EvidenceStorage` does
not carry it — an object store has no free bytes worth reporting, and a
contract that demanded a number would get an invented one. `LocalEvidenceStorage`
exposes `free_bytes()` on its own, the `LocalCapacity` protocol names that
ability, and `preflight_corpus` checks the reserve only against a backend that
has it. The corpus reconciliation gate runs against every backend alike.

### `EvidenceRoot` after B2.5

`EvidenceRoot` is the legacy layout as an object: an `EvidenceControlRoot`
(manifest, checkpoints, derived beneath it) that is also, structurally, an
`EvidenceStorage` (the local backend at the same path). `open_control_root`
returns it unchanged as the fallback. Production wiring opens both roots and
hands artifact-plane functions the root and control-plane functions the
control root; only the fallback makes them the same object, and `colocated`
says so.

| Responsibility | Disposition |
| --- | --- |
| local artifact backend (`.storage`, six delegated operations, `free_bytes`) | keep — this is the local backend's home |
| control-plane paths (`manifests`, `checkpoints`, `derived`, `manifest_path`) | inherited from `EvidenceControlRoot`; used only in the colocated fallback |
| `harvest()`, `preflight()`, `free_bytes()` module-level compatibility forms | keep for the colocated layout and tests; production wiring uses `harvest_artifacts` and `preflight_corpus` |
| `filing_path`, `submissions_path`, `fulltext_path` | deprecated compatibility helpers, zero production callers; remove once the tests that inspect the legacy layout no longer need them |
| the fallback itself | remove when no corpus uses the colocated layout |

### What did not change

Manifest fields, JSONL ordering, per-entry fsync, `Entry.path` as a logical
key, SHA-256 and byte semantics, checkpoint schema and resume rules, plan and
target-manifest formats and their digests, `ARTIFACT_PREFIXES`, reconciliation
counters, and every artifact-plane operation. No file was moved: an existing
corpus keeps its colocated layout until an operator relocates its control
files deliberately. `FTD_CACHE_DIR` remains a separate, redownloadable local
cache outside both planes.

## Future staging databases

A research staging database is 20–30 GB. **Create new ones under
`<volume>/stock-screener-data/staging/`**, not in the working tree, by pointing
`DATABASE_URL` at that path for the run. The boot disk keeps only the
authoritative one.

SQLite over USB works for the read-mostly access these databases get. An
external volume formatted ExFAT carries no POSIX permissions and no symlinks,
so keep archives as plain files.

## Evidence is provenance, not scratch

Filing documents are **raw evidence**. Two filings with the same name are not
necessarily the same bytes — earlier phases truncated documents at different
limits, and a sample of 150 same-named pairs found **49 with different SHA-256**.
Deduplicate on digest, never on filename, and keep the manifests: without the
URL, status, digest, byte count and retrieval time, a document cannot be shown to
be what it claims.
