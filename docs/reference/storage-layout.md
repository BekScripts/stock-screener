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
| `SEC_STORAGE_BACKEND` | `local` (reported as a default) | which backend holds the *artifact plane*: `local` or `b2` |
| `SEC_EVIDENCE_DIR` | **none** | root of the SEC evidence corpus when the backend is `local`; ignored by `b2` |
| `SEC_CONTROL_DIR` | **none** (legacy colocated layout) | root of the evidence *control plane* — manifest, checkpoints, plans, derived index; local under either backend |
| `B2_ENDPOINT_URL`, `B2_BUCKET`, `B2_KEY_ID`, `B2_APPLICATION_KEY` | **none** | the Backblaze bucket, when the backend is `b2` — see [B2.6](#b26-backend-selection-and-the-b2-configuration-boundary) |

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

## B2.6 backend selection and the B2 configuration boundary

B2.6 added the decision of *which* backend holds the artifact plane, and the
settings a Backblaze backend needs. B2.7 then filled the seam with
`B2EvidenceStorage` — see [B2.7](#b27-the-backblaze-backend). Nothing here
contacts Backblaze.

### Selection is explicit and fails closed

`SEC_STORAGE_BACKEND` is `local` or `b2`, and anything else is refused when
settings load. Nothing is inferred from what else is set:

| Situation | Result |
| --- | --- |
| selector unset | `local`, reported as `(defaulted — set SEC_STORAGE_BACKEND)` by every evidence command; kept for configurations that predate the setting |
| `local`, `SEC_EVIDENCE_DIR` unusable | the existing root rules apply unchanged — `EvidenceUnavailableError`, root never created |
| `local`, every `B2_*` value present | still `local`; credentials do not select a backend |
| `b2`, configuration complete, a local root also configured | `b2`; the local root is not opened, not touched, not a fallback |
| `b2`, configuration incomplete or malformed | `StorageConfigurationError` naming the setting, before any backend exists; **never** `LocalEvidenceStorage` in its place |
| `b2`, `SEC_CONTROL_DIR` unset or missing | `ControlPlaneUnavailableError`; the legacy colocated fallback belongs to `local` alone |

The backend chosen for a run is the backend for the whole run. No code path
writes artifacts locally "temporarily" when a remote backend is unavailable,
and none will: that would put evidence somewhere the manifest does not
describe.

### One construction point

`stock_screener.sec_evidence.wiring.open_evidence_layout(settings)` is where a
backend is built from configuration. It validates the selected backend,
opens the control root (for either backend — a `b2` run needs somewhere to
put its manifest before it needs a bucket), constructs the artifact backend,
and returns an `EvidenceLayout` whose `storage` is typed `EvidenceStorage`.
The CLI calls it once per command; everything after that line takes an
`EvidenceStorage` and cannot tell a directory from a bucket. Tests construct
backends directly.

`StorageConfigurationError` is a `providers.ConfigurationError`, so the CLI
reports it as one line with exit code 2. It is deliberately not a
`StorageUnavailableError`: that type means a backend that was configured and
then could not answer; this one is raised before any backend exists. For
`b2`, `open_evidence_storage` returns a `B2EvidenceStorage` bound to the
bucket, offline.

### What `b2` requires, and how it is checked

| Setting | Meaning | Offline check |
| --- | --- | --- |
| `B2_ENDPOINT_URL` | the S3-compatible endpoint Backblaze assigns, `https://s3.<region>.backblazeb2.com` | `https` scheme; a hostname; no userinfo; no query string or fragment. **No hostname is hardcoded** — an unfamiliar https host is accepted and left to the backend to qualify |
| `B2_BUCKET` | the bucket holding `filings/`, `submissions/`, `fulltext/` as key prefixes | a bucket *name*: 6 to 63 letters, digits or hyphens — refuses a URL, a path, a blank |
| `B2_KEY_ID` | the application key ID, used as the S3 access key ID | present and non-blank; held as `SecretStr` |
| `B2_APPLICATION_KEY` | the application key, used as the S3 secret access key | present and non-blank; held as `SecretStr` |

There is no `B2_REGION`. The signing region is part of the hostname Backblaze
assigns (`s3.us-west-004.backblazeb2.com` → `us-west-004`) and
`B2Configuration.region` derives it; a separate setting would be one more thing
to keep in step with the endpoint. If B2.7 meets an endpoint it cannot derive a
region from, that is the moment to add one.

Every missing setting is named at once. Validation is a presence and shape
check only — no authentication, no bucket HEAD, no listing — so a
misconfiguration surfaces as a one-line message when a command starts, not as
a stack trace on the first upload.

### Secrets

Both credentials are `SecretStr` in `Settings` and stay wrapped inside
`B2Configuration`. They do not appear in a `repr`, a log line, an error
message or status output; errors name the setting, never its value. Status
prints `endpoint: configured`, `bucket: configured`, `credentials:
configured` (or `MISSING`) and nothing more — not the endpoint, not the
bucket name. `test_storage_configuration.py::TestSecretsStayIn` checks each of
those surfaces with a known application key and asserts it is absent.

Credentials come from the environment through `Settings`, like every other
provider credential in this project. There is no credentials file, and
`.gitignore` covers `.env`, `.env.*`, `.evidence/`, `credentials.json`,
`*.credentials.json` and `*.pem` should one ever be made by hand.

### The future deployment, as configuration

```text
SEC_STORAGE_BACKEND=b2
SEC_CONTROL_DIR=/Users/<you>/stock-screener-control     # boot disk, local
B2_ENDPOINT_URL=https://s3.<region>.backblazeb2.com
B2_BUCKET=<bucket name>
B2_KEY_ID=<from the environment, never this file>
B2_APPLICATION_KEY=<from the environment, never this file>
```

`SEC_EVIDENCE_DIR` is not part of that deployment. The control plane stays
local; the manifest it holds records artifact keys that mean the same thing
in a bucket as they did in a directory.

### Recommendation for B2.7: the S3-compatible API through `boto3`

Three options were weighed for the backend that B2.7 will build.

| Option | For | Against |
| --- | --- | --- |
| **`boto3`/`botocore` against the S3-compatible API** | HEAD, GET, PUT, LIST and multipart are standard; retries (`standard` mode) and timeouts are configuration; streaming bodies; SigV4 signing that has been exercised against every S3-compatible store; `botocore.stub.Stubber` for unit tests, and the existing memory backend already proves the contract | a large dependency tree (botocore's service data is tens of megabytes); a second HTTP stack beside `httpx` |
| native `b2sdk` | large-file upload API; per-upload SHA-1 verification | a vendor-specific auth and API model; verifies SHA-1 where this project's contract is SHA-256; heavier than the S3 path for the four operations needed; no shared test convention |
| in-house SigV4 over `httpx` | no new dependency; one HTTP stack; `httpx.MockTransport` testing matching every other provider; `RetryPolicy` reuse | signing must be written and maintained; multipart would be ours to build; a signing bug surfaces as an opaque 403 |

**`boto3` on the S3-compatible API is the recommendation**, and B2.7 took
it. The deciding points were the standard S3 semantics for exactly the six
contract operations, and a property that falls out of SigV4 for free: a
signed PUT carries `x-amz-content-sha256` and the server rejects a body that
does not hash to it. That is a transport property; the persisted digest of
record is still computed here and verified by a HEAD — see B2.7. The corpus
has no object near the 5 GB single-PUT limit, so multipart was not what
decided it and is not implemented.

Two constraints on that backend, stated now so B2.7 inherits them:

- **ETag is never SHA-256.** `digest()` reads the object and hashes it;
  `write_bytes()` returns the digest of the bytes it sent. ETag is opaque
  metadata and no code maps it to a digest, even as a shortcut.
- **Nothing B2-specific reaches `EvidenceStorage`.** No `bucket`, `etag`,
  `endpoint`, `multipart` or `presigned_url` on the contract; whatever the
  backend needs internally stays internal.

### Least privilege for the application key

Create the key for the one bucket, after enabling the S3-compatible API on the
account (a key created before that, and the master key, do not work with the
S3 API). Grant:

| Capability | Needed | Why |
| --- | --- | --- |
| list files | yes | `list_prefix` — reconciliation and orphan detection |
| read files | yes | `read_bytes`, `digest`, `stat` — parsing and cache verification |
| write files | yes | `write_bytes` — harvesting |
| delete files | **no** | routine evidence operation never deletes; a manifest is append-only and an artifact is never removed. Grant it separately, temporarily, if a migration or cleanup explicitly needs it |
| list buckets | no | the bucket is named in configuration |

No credential is created in B2.6.

### Bucket layout

```text
<bucket>/
├── filings/<cik-padded>/<accession-no-dashes>/<document>
├── submissions/CIK<cik-padded>.json
├── fulltext/<query-digest>.json
└── control/            reserved: a future mirror of manifests/, checkpoints/,
                        plans/ and migration/ — a separate concern, not the
                        control plane's home
```

The artifact prefixes are the local layout's, byte for byte, because they are
the manifest's `path` keys. A manifest record does not name a bucket, an
endpoint or a backend, and will not: `Entry.path` is the same string whichever
backend stored the bytes.

## B2.7 the Backblaze backend

`B2EvidenceStorage` (`sec_evidence/b2.py`) implements the six-operation
contract over one B2 bucket through the S3-compatible API, using `boto3`. It
passes the same contract suite as the local and memory backends, and the
full reconciliation matrix produces identical counters over all three. It
has **not** been validated against a live bucket: every test runs over a
stateful fake client or a `botocore` stub, and no request has left the
machine. Live validation is B2.8, on a dedicated prefix, with explicit
authorization.

### Which call answers which operation

| Contract | S3 call | Absent | Everything else |
| --- | --- | --- | --- |
| `exists` | `HeadObject` | 404 → `False` | `StorageUnavailableError` — including 403 |
| `stat` | `HeadObject` | 404 → `ArtifactNotFoundError` | `StorageUnavailableError` |
| `read_bytes` | `GetObject`, body read whole, then closed | `NoSuchKey` → `ArtifactNotFoundError` | `StorageUnavailableError`, a short body included |
| `write_bytes` | `PutObject` then `HeadObject` | — | `EvidenceWriteError` |
| `digest` | `GetObject`, SHA-256 streamed over the body | `NoSuchKey` → `ArtifactNotFoundError` | `StorageUnavailableError` |
| `list_prefix` | `ListObjectsV2`, paginated | empty | `StorageUnavailableError` |

Nothing deletes, nothing lists buckets, nothing copies. The `S3Operations`
protocol in `b2.py` names exactly the four client calls the backend makes,
and a fake that lacks `delete_object` satisfies it — so "never deletes" is
checked by the type system as well as by a test that records every call.

The object key is the logical artifact key, unprefixed:
`filings/0000320193/000032019324000123/aapl-20240928.htm` in a bucket is
the same string it is in a directory, and a manifest written against one
reads unchanged against the other.

### The write trust model

```text
sha256 = SHA-256(data)                      computed here, before anything is sent
PUT  key  body=data  ContentLength=len  Metadata={sha256}
HEAD key  →  require ContentLength == len(data) and Metadata[sha256] == sha256
StoredArtifact(key, len(data), sha256)     returned only after both hold
```

A failed PUT, a failed HEAD, an object that vanished between the two, a
length that differs or a metadata digest that differs each raise
`EvidenceWriteError` and return nothing — so the harvester appends no
manifest record. `test_storage_neutral.py::TestHarvestTrustOrderingAgainstB2`
proves that for a failed upload, a truncated upload and a failed
verification HEAD: the manifest stays empty in all three.

Three things are deliberately not what they might look like:

- **ETag is not SHA-256** and is never read.
- **The `sha256` metadata is a verification aid, not the digest of record.**
  It is what the writer claimed at upload time; `digest()` never returns it.
  The manifest SHA remains authoritative, and it is the SHA of the bytes the
  harvester was handed.
- **`x-amz-content-sha256` is not evidence.** SigV4 signs a body hash and
  the server rejects a body that does not match it, which is a useful
  transport property; it is not persisted and is not what the manifest
  records.

`digest(key)` downloads the object and hashes it. That doubles the bytes
moved for a cache-hit check against a bucket, and it is the right trade in
this phase: a metadata claim that has stopped being true is exactly what a
digest check exists to catch. A verified-metadata shortcut can be
considered once live validation has shown how the bucket behaves.

### Uploads are single PUTs

`PutObject` carries at most 5 GiB. No SEC evidence artifact approaches it —
the largest filing documents are tens of megabytes — so multipart upload is
not implemented, and a payload past `SINGLE_PUT_LIMIT` is refused before any
request rather than handed to a code path that does not exist. If the limit
is ever met, multipart comes with its own SHA-256 computed over the whole
payload here, never a multipart ETag.

### The client, explicitly

| Setting | Value | Why |
| --- | --- | --- |
| credentials | from `B2Configuration`, passed to `boto3.session.Session` | never `~/.aws`, never `AWS_*`; a test sets those and shows they lose |
| region | derived from the endpoint host | an endpoint the region cannot be derived from is refused at construction, offline |
| signing | `s3v4` | what B2 accepts |
| addressing | path-style | deterministic URLs; no dependency on a bucket-name subdomain resolving |
| retries | `standard`, 3 retries (4 requests) | backoff with jitter on throttles, 5xx and connection faults; then `StorageUnavailableError`. No retry loop on top |
| connect timeout | 10 s | a bucket that does not answer a handshake in ten seconds is down |
| read timeout | 60 s | a minute of silence on a tens-of-megabytes object is a fault, not a large file |
| checksums | `when_required` | the SDK's newer default attaches a CRC to every PUT, which not every S3-compatible store accepts; this backend verifies its own SHA-256 |
| user agent | `compounder-radar-sec-evidence` | identification only |

Constructing the client is offline. `evidence status` still constructs no
backend at all and reports `configured (offline check only; no live probe was
made)`; `open_evidence_layout` constructs one and has spent no request when
it returns.

### Listing

`ListObjectsV2` with the SDK paginator, requesting `prefix/` so the server
narrows to the segment; every returned key is then checked to start with
that prefix anyway, so `filings/1` cannot list `filings/10/…` however the
server behaves. Keys are collected across all pages into a set and yielded
sorted: pages arriving out of order or repeating a key produce the same
output. Keys that are not canonical artifact keys — a dot-prefixed segment
(a backend's own temporary), a doubled separator — are not surfaced.

### Errors carry no secret

An error message names the operation, the key and the response code — never
the server's message text, which for a signature failure can echo the
canonical request. The backend holds the bucket and endpoint and no
credential; `repr` shows those two. Tests assert a sentinel application key
absent from `repr`, from every mapped exception and its cause, and from
captured logs.

### Application key, verified against the calls made

| Capability | Used by | Needed |
| --- | --- | --- |
| `readFiles` | `HeadObject`, `GetObject` | yes |
| `writeFiles` | `PutObject` | yes |
| `listFiles` | `ListObjectsV2` | yes |
| `deleteFiles` | nothing | **no** |
| `listBuckets` | nothing — the bucket is named in configuration | no |

### Dependencies

`boto3>=1.43.94` (runtime; resolves `botocore 1.43.94`, `s3transfer 0.19.2`,
`jmespath 1.1.0`) and `boto3-stubs[s3]` in the `lint` group for mypy
(`botocore-stubs`, `mypy-boto3-s3`, `types-s3transfer`). No `b2sdk`, no
`aiobotocore`, no `s3fs`.

## B2.8 the live canary

The first request that leaves the machine for Backblaze is made by the
canary, on purpose, and by nothing else first. `stock-screener evidence
b2-canary --confirm` (or `uv run pytest tests/live --live-b2 -p no:randomly
-s`) drives `sec_evidence/canary.py`: production wiring, a temporary control
root, every contract operation, one real harvest, a replay, a reconciliation,
and cleanup — all under `_canary/<run-id>/` and never under an artifact
prefix. It writes about two megabytes of synthetic, deterministic bytes and
deletes them. It fetches nothing from the SEC.

### Isolation without changing the production mapping

`_canary` is not an artifact prefix, so a production reconciliation never
sees a canary object, and a run id (`b28-<UTC stamp>-<random>`) is unique to
one invocation. The production key mapping — object key equals logical key —
is untouched: `CanaryNamespace` wraps the real backend and prepends the run
prefix on the way in and strips it on the way out, so the harvester, the
audit and the manifest see ordinary `filings/...` keys and behave exactly as
in production while the bucket sees `_canary/<run-id>/filings/...`. Both
layers validate every key; nothing the contract would refuse reaches the
wire.

### What one run proves, in order

| Step | Proves |
| --- | --- |
| list the empty run prefix | endpoint, TLS, credentials, bucket, `listFiles` — before any write |
| unknown key | `exists` False; `read_bytes` raises `ArtifactNotFoundError` |
| unsafe keys | refused with zero requests |
| write ×5 | absent before; receipt key, length and digest equal the bytes sent; PUT then HEAD verification |
| verify ×5 | present after; `stat` length; `read_bytes` byte-for-byte and SHA-256; `digest` from the download |
| listing | complete, sorted, recursive, one accession's listing excludes the other |
| harvest | no record before; one trusted record after, whose digest the bucket's bytes hash to |
| replay | `CACHE_HIT`, no fetch, no write |
| reconcile: clean | trusted = 6, no orphans, no missing, no mismatches; `zero_byte = 1` for the empty object — an expected diagnostic, not a fault |
| reconcile: orphan | one unrecorded object → `orphan_files = 1` |
| reconcile: missing | one record for an object never written → `orphan_records = 1` |
| cleanup | only `_canary/<run-id>/`; the prefix lists empty afterwards |

The run stops at the first step that does not hold and always attempts
cleanup. Cleanup is outside the evidence contract — `EvidenceStorage` still
has no delete — and `validate_cleanup_prefix` refuses anything but exactly one
`_canary/<run-id>/`: never the bucket, never the canary root, never an
artifact prefix. An application key without `deleteFiles` leaves the objects
in place and the report says so; that is not a failed canary.

### Offline first

`tests/unit/test_b2_canary.py` runs the whole flow against the fake client:
a passing run, a truncated upload, a bucket whose GET body lies, a dead
endpoint at preflight, a prefix that is not empty, cleanup refusals, and the
command with and without `--confirm`. `tests/live/test_b2_canary_live.py`
is the real thing and is skipped unless `--live-b2` is passed — `make check`
never passes it, needs no credential, and makes no Backblaze request.
`evidence status` still constructs no client.

### The first live run — 2026-09-15

Run `b28-20260915T233118Z-5a5c6689` (the command) and
`b28-20260915T233201Z-a9b49b3b` (the opt-in pytest path) both passed every
step against the configured bucket at `s3.us-east-005.backblazeb2.com`, with
`SEC_CONTROL_DIR=.evidence` and a temporary control root for the canary
manifest. Each run: 7 objects, 2,098,604 bytes, 7 PUT, 83 HEAD, 32 GET,
15 LIST, one batch DELETE of 7; every `attempt` count equalled its `call`
count, so the SDK retried nothing; cleanup left the run prefix empty. The
captured output was searched for the application key and key ID and held
neither. Afterwards `filings/`, `submissions/`, `fulltext/` and `_canary/`
all list zero objects, and the production control root holds no manifest.

Latencies seen (single run, informational): PUT+HEAD 267–667 ms per object,
HEAD ~50 ms, GET ~120 ms, the 2 MiB GET+digest ~580 ms, LIST ~130 ms.

To run it again after a credential rotation or an endpoint change:
`stock-screener evidence b2-canary --confirm`. The key needs `listFiles`,
`readFiles`, `writeFiles`, and `deleteFiles` only so the canary can remove
its own objects.

## B2.9 the migration plan

`stock-screener evidence migration-plan` answers one question per trusted
artifact — *how would this one get into the bucket, and can we prove it
arrived?* — and writes the answers under the control root's `migration/`.
It moves nothing: no PUT, no DELETE, no SEC request. `sec_evidence/migration.py`
is the planner; the executor is a later phase.

### Trust, replica, reproduction

| Thing | Role |
| --- | --- |
| `manifests/evidence.jsonl` | **the source of trust.** Migration starts from its trusted entries and from nothing else. It is never rewritten because storage changed, and never forked into a second manifest. |
| the B2 bucket | **the artifact backend** — where the bytes live once migrated. Its objects are evidence only when a trusted entry names them *and* their bytes hash to `Entry.sha256`. |
| `migration/sec_b2_migration_plan_v1.jsonl` | **replication state**, not trust: what should happen to each key, and later what did. |

The migration reproduces *the bytes already trusted*: `Entry.bytes` and
`Entry.sha256`. A local copy is used only if it hashes to them; a refetch is
accepted only if the bytes downloaded today hash to what was trusted then.
Different bytes from the same URL are a `REFETCH_SHA_MISMATCH`, never a
quietly new artifact.

### One action per trusted key

The trusted set is built with the manifest's own replay semantics — the
latest word per URL wins; a `FAILED` after a `DOWNLOADED` untrusts; two URLs
vouching for one key with the same bytes are one key — so no JSONL line is
ever counted as an artifact. Every trusted key then receives exactly one
action, in this order of precedence:

| Order | Observation | Action |
| --- | --- | --- |
| 1 | the manifest is ambiguous about the key (two URLs, different bytes; no digest; no length; unsafe key) | `CONFLICT` |
| 2 | the bucket holds it and the **downloaded** bytes hash to the manifest digest | `REMOTE_VERIFIED` |
| 2 | the bucket holds it with the wrong length or digest | `CONFLICT` |
| 3 | the local source holds it, read and hashed, and matches | `LOCAL_VERIFIED_COPY_CANDIDATE` |
| 3 | the local source holds it at the recorded length, not hashed | `LOCAL_COPY_CANDIDATE` |
| 3 | the local source holds it with the wrong length or digest | `CONFLICT` |
| 4 | none of the above, and the manifest carries URL, digest and length | `REFETCH_REQUIRED` |
| 4 | none of the above, and it does not | `SOURCE_UNRECOVERABLE` |

`sum(actions) == trusted keys`; the plan has one row per key, sorted by key;
the same inputs reproduce the same bytes and the same digest. Source
observations are part of the inputs — the plan digest differs between a
day the old volume answered and a day it did not, and the summary records
which.

### The old volume is asked, never walked

The planner never enumerates the legacy root. It asks about manifest keys
only, one at a time, in key order, as much as the probe allows:

| `--probe` | Asks | Default |
| --- | --- | --- |
| `none` | nothing; every key `UNKNOWN` | |
| `metadata` | existence and length per key | **yes** |
| `verify` | reads and hashes every present key | explicit, because the volume is unreliable |

The first `StorageUnavailableError` ends the probe: keys already answered
keep their answers, every later key is `UNAVAILABLE`, and no further request
is made against a volume that just failed. A plan is always produced;
`--source-root` overrides the configured `SEC_EVIDENCE_DIR`.

### The bucket is listed, never written

Remote discovery is `list_prefix` over `filings/`, `submissions/` and
`fulltext/` — three requests when the prefixes are empty, which is the
expected state. When they are not, each listed key a trusted entry claims is
verified by `stat` *and* by a downloaded digest (a HEAD's metadata is a
claim), and each listed key no entry claims is a **`REMOTE_ORPHAN`**: the
plan is still written, but the command exits non-zero and execution is
blocked until every orphan is explained. `_canary/` is not an artifact prefix
and is never listed. A listing failure stops the planner; an uncertain
bucket is not planned against.

### Files

```text
<SEC_CONTROL_DIR>/migration/
├── sec_b2_migration_plan_v1.jsonl          one row per trusted key, key order
├── sec_b2_migration_plan_v1.sha256         "<sha256>  sec_b2_migration_plan_v1.jsonl"
├── sec_b2_migration_plan_v1.batches.jsonl  contiguous batches, each with its own digest
└── sec_b2_migration_plan_v1.summary.json   counts, source probe and fault, remote findings
```

A row carries `plan_version`, `key`, `url`, `expected_bytes`,
`expected_sha256`, `artifact_kind`, `cik`, `accession`, `form`,
`filing_date`, `local_source_status`, `remote_status`, `action`, `reason` —
provenance the manifest already holds and the planner's observations.
Nothing else: no credential, no path, no bucket. Batches are 1,000 rows of
the sorted plan (`--batch-size`), each with `number`, `first_key`,
`last_key`, `targets`, `logical_bytes` and a SHA-256 over its rows' lines.

### The execution ledger, designed and not built

`ExecutionState` — `PENDING`, `COPYING`, `UPLOADED` (the backend's HEAD
verification passed), `REMOTE_VERIFIED` (an independent download hashed to
the manifest digest), `FAILED` — is the ledger's vocabulary, and
`FailureCategory` names why: `LOCAL_MISSING`, `LOCAL_READ_ERROR`,
`LOCAL_SHA_MISMATCH`, `REFETCH_HTTP_ERROR`, `REFETCH_BYTE_MISMATCH`,
`REFETCH_SHA_MISMATCH`, `B2_PUT_ERROR`, `B2_VERIFY_ERROR`,
`B2_DIGEST_MISMATCH`, `REMOTE_CONFLICT`, `REMOTE_ORPHAN`. Planner
classification and execution state are separate fields; a row planned
`REFETCH_REQUIRED` may end `REMOTE_VERIFIED` and the plan row does not
change.

The chain a later phase must follow, per row:

```text
trusted entry
→ obtain candidate bytes   (1 REMOTE_VERIFIED: skip; 2 verified local; 3 local, hash first;
                            4 refetch the URL; 5 unresolved: stop and report)
→ SHA-256(candidate) == Entry.sha256 and len == Entry.bytes, or FAILED
→ B2 write_bytes           (PUT, then the backend's own HEAD verification)
→ digest(key) == Entry.sha256   (independent download)
→ ledger REMOTE_VERIFIED, durable, before the next row
```

Idempotent by construction: a key the bucket already holds with the right
digest is skipped; a crash resumes from the ledger, never from zero; no
verified object is uploaded twice. Rollback is never "delete remote
objects" — a failed migration leaves verified objects intact, and the key
needs no delete capability beyond the canary's.

### Verification cost, from the canary

Whether to download every migrated object once and hash it, or a sample.
The recommendation is **full**: this corpus is provenance-critical and a
metadata claim is what a digest check exists to catch. The cost, from the
last known corpus shape (about 57,000 trusted artifacts, 38.8 GB, average
680 KB — the manifest itself will replace these numbers) and the B2.8
latencies (PUT+HEAD 0.27–0.67 s per object; GET of a small object about
0.12 s; a 2 MiB GET about 0.58 s):

| | Requests | Bytes moved | Sequential time, rough |
| --- | --- | --- | --- |
| upload + HEAD | ~57k PUT, ~57k HEAD | ~39 GB up | 8–10 h |
| full digest verification | ~57k GET | ~39 GB down | 4–6 h |
| listing per batch | ~60 LIST | — | minutes |

Roughly 170,000 requests and one full pass of the corpus in each direction,
comfortably parallelisable across batches. Sampling would save the download
pass and nothing else; it is not worth the provenance it gives up.

### Control-plane recovery files

Execution needs these to exist somewhere other than the artifact volume:
the evidence manifest, the migration plan and its digest, the frozen
expansion plan and its remainder, the batch manifests, the checkpoints, and
`PROJECT_STATE.md`. As of this checkpoint only `PROJECT_STATE.md` is on the
boot disk; the rest lived beside the artifacts on the failed volume and are
**absent from `SEC_CONTROL_DIR`** — see the phase report. They are copied
there by an operator, deliberately, before any migration runs.

### Status — 2026-09-15

`evidence migration-plan` against the configured control root reported
**BLOCKED — AUTHORITATIVE MANIFEST UNAVAILABLE**: `.evidence/manifests/
evidence.jsonl` does not exist, the legacy root `/Volumes/BEK/...` is not
mounted, and trust is never reconstructed from files, checkpoints or bucket
objects. The read-only remote audit found `filings/`, `submissions/` and
`fulltext/` all empty. No plan was written. The planner is complete and
tested offline; it runs the moment the manifest is placed under the control
root.

## B2.9X the second generation

The first generation is lost. The external drive that held the corpus, the
manifest, the frozen Plan A3 and its batch manifests is permanently
unavailable, and nothing is coming back from it. **Generation V1 is history**:
its numbers survive in reports, not in records, and no report creates trust.
`stock-screener evidence generation-plan` starts **Generation V2** from what
survived on the internal disk, and says exactly what that can reproduce.

### What survived

| Input | Where | Status |
| --- | --- | --- |
| production universe — 8,919 identities, total order, digest `b8eaed3e…` | `stage9k.db` via `production_universe()` | **reproduces exactly** |
| stored CIKs — 402 delisted securities | `historical_securities.cik` | surviving |
| ticker → CIK for the other 8,517 | never persisted; resolved from the SEC ticker map at harvest time | network |
| submissions catalogues; filing documents | the SEC | network |
| filing acceptance catalogue — 5,889,860 rows | `stage9k.db filing_acceptance` | surviving |
| research universe months — 268,780 rows, the frozen class attribution | `stage9k.db research_universe_months` | surviving |
| derived evidence index; cover rows; form yields; sizes | built from the corpus | lost — rebuilt |
| Plan A3 inputs, and the harness that assembled `PlanningInputs` | working files, never committed | lost — `plan.py` holds the algorithm only |
| frozen Plan A3 (61,941 targets), remainder, 61 batch manifests | the drive | lost — the digest `53e3f0ca…` survives as a comparison target |
| FTD archives — 433 source files | the drive | lost; observations survive in the database; public and redownloadable |

### What that decides

- **The baseline is PARTIAL offline.** The identity plan is exact (every
  identity, its chunk, its stored CIK). The 402 stored-CIK submissions
  requests are exact. Everything else the first generation requested —
  8,517 filer resolutions, every submissions catalogue, every filing
  document — comes from the SEC in a later, explicit phase.
- **Plan A3 is `MISSING_PREREQUISITE`.** Its inputs were the parsed baseline
  corpus, and its assembly code never reached the repository. It is
  regenerated after the baseline is rebuilt and re-indexed, and the result
  is *compared* to the frozen digest. A match is `A3_REPRODUCED_EXACTLY`; a
  difference is `A3_DIGEST_DIFFERS` and is reported, never forced — the
  planner algorithm is not changed to reach an old hash.
- **Target reproducibility is not byte reproducibility.** Even an exact A3
  match says nothing about whether the SEC serves the same bytes today. V2
  trust begins only when V2 bytes are fetched, hashed and recorded.

### Generation V2 starts at zero

`.evidence/` is the V2 control root. Its manifest holds no records; its
checkpoints directory is empty; the bucket's artifact prefixes hold nothing
(re-verified read-only). `v2_progress` counts a `COMPLETE` checkpoint only
when the manifest vouches for at least as many artifacts as the checkpoint
claims; a first-generation checkpoint that wanders in over an empty manifest
is reported as inconsistent, not honoured. No V1 record is ever injected.

Artifact keys do not change between generations:
`filings/<cik>/<accession>/<document>` is the same key in V1, in V2 and in
the bucket, and the generation id is metadata, never part of a key.

### Files

```text
<SEC_CONTROL_DIR>/plans/generation_v2/
├── base_identities.jsonl          8,919 rows in universe order: position, chunk, bucket,
│                                  symbol, ids, cik, resolution, submissions_url
├── base_identities.sha256
├── base_targets_offline.jsonl     the 402 stored-CIK submissions requests
├── base_targets_offline.sha256
├── prerequisites.json             every rebuild input: SURVIVING / NETWORK / MISSING
└── generation.json                generation id, commit, database, universe digest,
                                   plan digests, A3 status, V2 progress, bucket counts
```

The plans and their digests reproduce byte for byte from the same database;
`generation.json` carries `created_at` and is the one file that differs
between runs.

### The rebuild sequence

```text
V2.1  resolve filers      the 402 stored CIKs offline; the 8,517 others from the SEC
                          ticker map, recorded per identity, never guessed
V2.2  baseline harvest    submissions catalogues, then filing documents (cap per
                          security, most recent first) — SEC → SHA-256 →
                          B2EvidenceStorage.write_bytes → HEAD verification →
                          manifest append on the control root; chunks of 250,
                          checkpoints per chunk, all starting PENDING
V2.3  re-index            build_index over V2 artifacts; evidence dates from
                          filing_acceptance
V2.4  coverage + reach    gaps from research_universe_months; class-aware
                          reachability from V2 cover rows
V2.5  regenerate Plan A3  the planner over V2 inputs; compare the digest to
                          53e3f0ca…; report, never force
V2.6  A3 expansion        directly to the bucket, batches of 1,000, checkpoints
V2.7  terminal audit      full reconciliation and full remote digest verification
```

Steps V2.1–V2.2 need the SEC. Nothing in this checkpoint contacted it.

### Control-plane durability, made a requirement

V1 had one copy of its control state and lost it. V2 keeps two: the control
root on the internal disk, and — before any large-scale harvest — a mirror
of the manifest, the checkpoints, the batch manifests and the generation
metadata after every completed batch, under a `control/` prefix in the
bucket or another independent location. That mirror is not built here; it is
a hard requirement of V2.2, recorded so it cannot be skipped.

### The migration planner's place

`evidence migration-plan` (B2.9) remains correct and useful for replicating a
corpus whose manifest exists. It cannot plan V1's migration, because V1's
manifest does not exist, and it is not the path for V2, which is built
directly into the bucket.

## B2.10 the rebuild canary

One chunk of the second generation, built from the SEC into the bucket and
proven at every step before the baseline rebuild is authorised.
`stock-screener evidence rebuild-canary --live` is the entrypoint; it
requires `SEC_STORAGE_BACKEND=b2`, the explicit `--live`, and chunk `000`
only. It never advances to chunk 001.

### The sequence

```text
preflight    V2 progress from the control root: 0 trusted, 0 completed, no
             inconsistent checkpoint; every artifact prefix in the bucket empty
mirror       generation.json and the offline plans (digests verified against
             their .sha256 companions) into control/SEC_EVIDENCE_GENERATION_V2/,
             each read back and hashed
resolve      one request for the SEC ticker map, preserved raw with a sidecar;
             all 8,919 identities resolved STORED → SEC_TICKER_MAP → UNRESOLVED,
             twice, byte-identical; frozen as filer_resolution_v1.jsonl + .sha256
freeze       chunk 000 = positions 0–249 of the universe order, nothing hand-
             picked; identities.jsonl + .sha256; checkpoint PENDING carrying
             the identity, resolution and generation digests in its notes
mirror       resolution, ticker map, chunk identities; manifest snapshot,
             checkpoint and summary snapshots
pass 1       submissions catalogues, one filer each — a filer shared by several
             identities (share classes, a delisted symbol whose company is
             still listed) is one catalogue
pass 2       filing targets derived from the trusted catalogues: FILING_FORMS
             only, newest first, cap 6 per filer, one target per (CIK,
             accession); derived twice, byte-identical; frozen and mirrored
             before the first document is fetched
converge     every frozen target replayed: 0 fetches, 0 bytes, 0 PUTs, every
             target CACHE_HIT — which means every artifact downloaded and
             hashed again
verify       full reconciliation with every artifact digest verified against
             the bucket; zero_byte must be 0; trusted must equal the targets
complete     checkpoint COMPLETE; final snapshots mirrored; recovery proven
```

The run stops at the first step that does not hold, records why in the
checkpoint's notes, saves the checkpoint, and writes the mirror ledger. A
rerun resumes: the preserved ticker map is reused, the frozen files must
reproduce under the checkpoint's digests, and the harvest passes find
their trusted artifacts as `CACHE_HIT`.

### The chain, one link stricter

```text
SEC fetch → SHA-256 of the received bytes → write_bytes (PUT, then HEAD:
length and digest metadata) → digest(key) (GET, hashed) == fetched digest
→ manifest append → state.record
```

`harvest_artifacts(verify_written=True)` adds the read-back. It is a
generic flag — a local directory re-reads the file, a bucket downloads the
object — and no code asks which backend it holds. Its regression shows what
it exists for: a bucket that holds different bytes of the same length after
a PUT passes the HEAD check and fails the read-back, and the result is zero
manifest rows, zero `state.record`, and the object left in place for
reconciliation to find. A failed remote digest creates no trust.

Every SEC failure is a policy, not a surprise: a `403` stops the run
before the next request (targets go through the harvester one at a time so
the stop is immediate, not at the end of the pass); a sustained `429`
stops it the same way; a `503` is retried within the client's bounded
policy; a `200` with an empty body is `FAILED`, never stored; anything
still failed after a second pass stops the run with what did succeed left
trusted and nothing rolled back.

### The mirror

`control/SEC_EVIDENCE_GENERATION_V2/` in the bucket is the second copy of
the control plane. It is not an artifact prefix: reconciliation never lists
it, so a mirrored file can never be an orphan, a trusted artifact, or
evidence. Its trust is full read-back — local SHA-256 → PUT → HEAD → GET,
hashed, equal — and an existing remote key is never overwritten: identical
bytes are a `MIRROR_HIT` at no PUT; different bytes are a `CONFLICT` and
the run stops.

Mutable files are mirrored as immutable snapshots. The bytes go to a
content-addressed key (`manifests/evidence-<sha16>.jsonl`), so identical
bytes mirrored again are one object; a descriptor stamped to the
microsecond (`manifests/evidence-<stamp>.snapshot.json`) names that key,
its digest, its size and the run's facts. Recovery lists the descriptors,
takes the newest, reads the file it names, and refuses it unless the bytes
hash to what the descriptor recorded. Same for checkpoints and chunk
summaries under `chunks/000/`.

```text
control/SEC_EVIDENCE_GENERATION_V2/
├── generation.json
├── plans/                      base_identities, base_targets_offline (+ .sha256), prerequisites.json
├── prerequisites/              company_tickers.json (raw), company_tickers.meta.json
├── resolution/                 filer_resolution_v1.jsonl, .sha256, .summary.json
├── manifests/                  evidence-<sha16>.jsonl; evidence-<stamp>.snapshot.json
└── chunks/000/
    ├── identities.jsonl, identities.sha256
    ├── filing_targets.jsonl, filing_targets.sha256
    ├── checkpoints/            chunk-000000-0250-<sha16>.json; <stamp>.snapshot.json
    └── summaries/              summary-<sha16>.json; <stamp>.snapshot.json
```

`plans/generation_v2/mirror_ledger.jsonl` on the control root records every
mirror operation with its outcome and verification time; `evidence status`
reads it to report the mirror as established, and nothing else about the
bucket, without a request.

### Resolution is not guessing

A stored CIK wins. A live company's symbol is looked up in the ticker map,
which describes who holds the symbol *now* — correct for a company listed
now. A delisted security without a stored CIK stays `UNRESOLVED` even when
its symbol is in the map, because the map names today's holder, not the
security that mattered on the day. That is stricter than the first
generation's `resolve_ciks`, which asked the map for every symbol; the
difference is counted (`delisted_symbols_in_map_declined`) and reported,
never corrected back.

### Files

```text
<SEC_CONTROL_DIR>/plans/generation_v2/
├── prerequisites/company_tickers.json          the SEC ticker map, raw; .meta.json; .sha256
├── filer_resolution_v1.jsonl                   8,919 rows: position, chunk, bucket, symbol,
│                                               ids, cik, source, reason
├── filer_resolution_v1.sha256 / .summary.json
├── chunks/000/identities.jsonl (+ .sha256)     the 250 frozen identities with their resolution
├── chunks/000/filing_targets.jsonl (+ .sha256) the frozen filing targets
├── chunks/000/summary.json                     counts and digests, rewritten per stage
└── mirror_ledger.jsonl                         every mirror operation, appended per run
<SEC_CONTROL_DIR>/checkpoints/chunk-000000-0250.json
<SEC_CONTROL_DIR>/manifests/evidence.jsonl      the V2 manifest
```

### The live run — 2026-09-16, in two invocations

The first invocation ran 01:46:58–02:16:33 UTC against `stage9k.db`
(read-only; digest unchanged before and after) with
`--expect-universe-sha b8eaed3e…`.

| Step | Result |
| --- | --- |
| preflight | trusted 0, completed 0, bucket `{filings: 0, submissions: 0, fulltext: 0}` |
| mirror offline plans | 6 files `MIRRORED`, each read back |
| resolve filers | one request; ticker map 797,759 bytes, `82cd5fd9…`; 8,919 rows: 402 `STORED`, 5,646 `SEC_TICKER_MAP`, 2,871 `UNRESOLVED` (135 delisted symbols declined although in the map); `filer_resolution_v1.jsonl` `161429f5…`, deterministic |
| freeze chunk | positions 0–249: 167 LIVE, 83 DELISTED; 15 `STORED`, 165 `SEC_TICKER_MAP`, 70 `UNRESOLVED`; 180 resolved identities → **165 filers** (share classes, one delisted/live pair); `identities.jsonl` `14f41c86…`; checkpoint `PENDING` |
| pass 1 | 165 catalogues `DOWNLOADED`, 17,319,117 bytes, 185 s; each read back before its row |
| derive | 936 targets over 936 `(CIK, accession)` pairs — 587 8-K, 213 10-Q, 70 20-F, 30 40-F, 23 10-K, 13 20-F/A — derived twice, identical; `filing_targets.jsonl` `0c67536b…`; mirrored before the first document |
| pass 2 | 936 documents `DOWNLOADED`, 885,706,263 bytes, 1,315 s; each read back before its row |
| manifest | 1,101 records, 1,101 trusted, 0 `FAILED`; snapshot `evidence-4328d3b4…` mirrored |
| converge | **stopped** after 754 of 1,101 `CACHE_HIT` (165 catalogues, 589 documents, every digest equal): the bucket answered `AccessDenied` to a GET |
| verify, complete, recovery | not reached; checkpoint `PASS2_DONE` with the stop recorded in its notes |

SEC: 1,106 metered attempts for 1,102 documents — one `503` retried, two
large 20-F bodies re-read after a timeout, one attempt with no response —
all within the client's bounded policy; zero `403`, zero `429`, zero empty
bodies. 903,025,380 bytes stored; 27.6% of the request budget, 42.0% of
the byte budget. Bucket: 1,136 PUT, 1,136 HEAD, 2,258 GET, 3 LIST, no
delete. Mirror: 35 `MIRRORED`, 2 `MIRROR_HIT`, 0 `CONFLICT`, 6,006,205
bytes. Credential audit over the log and every control file: 0.

The stop was Backblaze's account-level daily cap, verified afterwards with
one HEAD and one GET: *"download bandwidth or transaction (Class B) cap
exceeded. See the Caps & Alerts page to increase your cap."* Not a
permissions fault, not a trust fault: nothing was recorded that the bucket
had not proven, and the 754 artifacts replayed before the denial hashed to
their rows.

With the caps removed, the **same command resumed at 02:46:49 UTC** over
the existing state — checkpoint `PASS2_DONE`, 1,101 trusted, the preserved
ticker map, the frozen resolution and chunk files (every one verified
against its `.sha256` and its mirror copy first) — and completed at
03:14:07:

| Step | Result |
| --- | --- |
| preflight (resume) | trusted 1,101, bucket `{filings: 936, submissions: 165}`; reconciliation clean before any step |
| mirror | offline plans, resolution, ticker map, chunk plans: 26 `MIRROR_HIT`, 0 `CONFLICT`; 19 new snapshot objects (descriptors, checkpoint and summary states) |
| resolve, freeze, derive | ticker map reused from disk (no request); resolution, identities and filing targets reproduced byte for byte under the checkpoint's digests |
| pass 1, pass 2 | 165 + 936 `CACHE_HIT`; 0 requests, 0 bytes |
| converge | **1,101 / 1,101 `CACHE_HIT`, 0 fetches, 0 bytes, 0 PUTs** |
| verify | full reconciliation, every artifact downloaded and hashed: trusted 1,101 = objects 1,101; orphans 0 / 0, byte and hash mismatches 0, path violations 0, zero-byte 0, duplicate URLs 0; 885,706,263 logical bytes |
| complete | checkpoint `COMPLETE` at 03:14:07 with accounting `{trusted: 1101, submissions: 165, filings: 936, manifest_records: 1101}` and `verification.full_corpus = 1101`; final manifest snapshot `evidence-4328d3b4…` (unchanged bytes, `MIRROR_HIT`) with a new descriptor; checkpoint and summary snapshots mirrored and read back |
| recovery | 54 mirrored objects; required files present and hashed; newest manifest snapshot names 1,101 artifacts = 1,101 bucket objects; mirrored checkpoint `COMPLETE` — and, independently, a bucket-only script reconstructed the generation, plans, ticker map, resolution, chunk membership, targets, manifest and checkpoint from the mirror and hashed all 1,101 evidence objects against the mirrored manifest |

Resume traffic: **SEC 0**. Bucket: evidence PUT **0**; evidence HEAD 6,606
(two reconciliations at three HEADs per artifact); evidence GET 3,633 (pass
replays 1,101, derivation 330, convergence 1,101, verification 1,101);
control PUT 19, control GET 72, LIST 15. Mirror ledger over both
invocations: 54 `MIRRORED`, 28 `MIRROR_HIT`, 0 `CONFLICT`. Credential
audit over both logs and every control file: 0. Database digests unchanged.

**Caps are an operational requirement.** One chunk downloads its corpus
about three times (read-back, replay, verification — ~2.7 GB for chunk
000) and a resume about four; each reconciliation costs three HEADs per
artifact. Backblaze's default caps refused that; with the caps removed the
same run completed. Expect 35 further chunks at ~0.9 GB stored, ~1,100 SEC
requests and ~26 minutes of harvest each, plus ~20 minutes of verification.

Defects found and fixed along the way, none of them in the trust chain:
two snapshots of identical bytes within one second collided on the
descriptor key with different facts (the scheme above is the fix); the
chunk summary lost its identity counts when rewritten by a snapshot step;
two identities sharing a filer would have produced duplicate filing
targets and failed the trusted count — hence one catalogue per filer and
one target per `(CIK, accession)`; and a resume would have overwritten the
first invocation's per-pass request and byte accounting with zero and
walked the checkpoint back through `PASS1_DONE` — accounting is now a
delta accumulated across resumes, and the stage never regresses. The
checkpoint's recorded `requests` (`submissions: 166`, `filings: 1106`) are
the first invocation's cumulative meter readings, kept as written.

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
