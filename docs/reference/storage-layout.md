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
| `SEC_EVIDENCE_DIR` | **none** | root of the SEC evidence corpus |

`SEC_EVIDENCE_DIR` having no default is the point, not an oversight.

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

`SEC_EVIDENCE_DIR` points at `<volume>/stock-screener-data/sec`.

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
