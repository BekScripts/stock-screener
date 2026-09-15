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
