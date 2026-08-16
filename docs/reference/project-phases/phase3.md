---
type: reference
title: Phase 3 — grounded AI research
---

# Phase 3 — grounded AI research

Phase 3 turns a ranked shortlist into research a person can check. A model reads
evidence the pipeline assembled, writes claims that cite it, and validation drops
anything the evidence does not support before a word of it is stored.

The rule the whole phase is built around: **the model may explain the evidence and
may not add to it.**

## The workflow

```text
Alpaca + EDGAR
  → deterministic scan
  → CompounderScore V1.1
  → research candidate selection
  → SEC filing excerpts
  → ResearchBrief
  → Claude DraftReport
  → validation
  → grounded ResearchReport
  → cache / persistence
```

Each arrow is a boundary something is not allowed to cross.

**Alpaca + EDGAR → scan → score.** Unchanged by this phase. CompounderScore
V1.1 is read-only to everything downstream: research explains a stored snapshot
and can never modify, recompute or override one.

**Candidate selection.** At most 25 companies from the latest ranking — the top
ranked, the score movers, a few hidden gems — chosen deterministically from
stored scores. `stock-screener research candidates` shows the list without
spending anything.

**SEC filing excerpts.** A separate, explicit pass
(`research update-filing-text`) reads up to six filings per company and stores
the sections worth quoting. Selection is form-aware — the latest 10-K, the two
latest 10-Qs, then 8-Ks — so a busy month of governance filings cannot crowd out
the report that says what the company does. Extraction is deterministic pattern
matching: headings located, text taken verbatim, nothing summarised. A section
that cannot be located confidently is absent, because a wrong excerpt is worse
than a missing one.

**ResearchBrief.** Assembled from the database alone — no provider is reachable
while a brief is built, and every input is bounded by the `score_date` of the
snapshot being explained, so last week's score is never explained with this
week's numbers. The brief carries the score breakdown, derived metrics, reported
quarters, filing metadata, up to five filing excerpts, and an explicit list of
what is unknown. Its SHA-256 fingerprint is the cache key.

**Claude DraftReport.** One request per company, sequential, through a provider
boundary that returns contract types only. The wire schema requires `claims`;
before it did, the model could satisfy the schema with an empty object, and 69%
of calls came back unusable.

**Validation.** Every claim is checked before anything is stored: citations must
resolve, figures must appear in the evidence, bases must suit the section, no
claim may recommend an action, and an over-long claim is dropped whole rather
than truncated. Only `validate_report` and `failed_report` construct a
`ResearchReport`, and `save_report` accepts nothing else — so an unvalidated
draft cannot reach the database even by mistake.

**Cache and persistence.** A report is stored against its brief fingerprint,
score version and prompt version. Re-running a company whose evidence has not
changed costs nothing; changing the evidence, the score or the prompt is a cache
miss by construction.

## Evidence namespaces

Every citable item carries its kind in its id, so a claim's grounding is legible
without looking anything up.

| Prefix | Meaning |
| --- | --- |
| `S.` | a line of the stored CompounderScore breakdown |
| `M.` | a derived metric |
| `F.` | a reported financial period |
| `D.` | **filing metadata** — this filing exists, on this date, at this URL |
| `X.` | **filing text** — a verbatim excerpt of one section |
| `E.` | a field supplied by the optional metered provider |

`D.` and `X.` are deliberately separate. An accession number proves a company
filed something and proves nothing about what it says, so an `EXTRACTED` claim
must cite an `X.` excerpt; citing `D.` alone is rejected. Numbers work the same
way: a figure from filing text is available **only to a claim citing the excerpt
it appears in**, never pooled across a brief.

## Guardrails

- **Missing is not zero**, throughout — as everywhere else in this codebase.
- **A provider failure is a `FAILED` report, never an exception that ends a
  run.** Scanning, scoring and ranking never depend on a model being reachable.
- **A permanent request failure aborts the run** rather than writing one
  `FAILED` row per company for a defect in the request we built.
- **A run stops before it overspends.** `RESEARCH_MAX_RUN_COST_USD` (default
  `$5.00`) is checked before each company against a pessimistic price for the
  request about to be made; companies past the limit are skipped, not failed.
- **Validation never repairs.** It drops, and records why.

## Production defaults

| Setting | Value |
| --- | --- |
| `RESEARCH_PROVIDER` | `anthropic` (one real provider; `mock` for tests) |
| `RESEARCH_MODEL` | `claude-sonnet-5` |
| `RESEARCH_EFFORT` | `medium` |
| `RESEARCH_MAX_OUTPUT_TOKENS` | `8000` |
| `RESEARCH_MAX_RUN_COST_USD` | `5.00` |

Both generation settings were chosen by experiment. High effort produced zero
usable reports out of four attempts — the token ceiling covers reasoning as well
as output, so a harder-thinking model ran out of room mid-document. A 16,000
ceiling changed nothing, because no healthy generation has ever exceeded ~4,700
output tokens.

## Observed cost and reliability

From the final validation batch of 19 candidates:

| | |
| --- | --- |
| substantive generations | 17 of 18 live calls (94%) |
| claims accepted / dropped | 606 / 33 |
| unsupported figures, citations, advice | 0 / 0 / 0 |
| total cost | ~$1.64 |
| cost per company | ~$0.09 |

## Known limitations

Documented rather than fixed. None blocks normal use; each is a thing to
recognise rather than be surprised by.

**Occasional zero-claim generation (~5% observed).** A call sometimes returns a
schema-valid draft with no claims. It costs that company its report — a `PARTIAL`
row of honest `UNKNOWN`s — and nothing else; the run continues. There is no
automatic retry, deliberately: the failure is cheap and re-running one company is
a single command.

**`CLAIM_TOO_LONG` drops long claims.** An accepted claim may not exceed 240
characters, and validation drops rather than truncates — rewriting the model's
sentence would make the stored text something nobody wrote. Filing evidence
produces longer sentences, so this fires more often now (12 claims across 19
companies) than it did before.

**Some `EXTRACTED` claims use a basis the section forbids.** `major_risks` in
particular: the model wants to cite risk-factor text there, and the section's
allowed bases were fixed before filing text existed. The claim is dropped, which
is correct under the current policy and arguably too strict.

**Occasional numeric false positives.** Seven across the final batch, none an
invention: display rounding ("90 million" for 89,854,000), the literal `2.02`
read out of "Item 2.02", and one score-scale phrasing the `of 100` exemption does
not recognise ("out of a possible 100").

**Some companies have no `business` section.** Nine of nineteen do. A 10-K
outside the eight-filing index window cannot be read, so those companies describe
themselves through MD&A instead — usually enough, occasionally not.

**Grounding proves provenance, not truth.** Validation establishes that a claim
traces to the brief it was given. It cannot establish that the brief was right.
A `DETERMINISTIC` claim is exactly as correct as the extraction behind it, and a
wrong figure produces a wrong claim that passes every check — correctly, because
the claim does faithfully report what it was shown.

This is not hypothetical. An EDGAR chain that could not see a filer's revolver
and term loan stored one company's debt as `0`, and the report said debt "rose
sharply from 0" and that "new financing was taken on". Both sentences were
properly grounded. Both were false: the debt had been there throughout and was
being *paid down*. See the debt and free-cash-flow notes in
[metrics](../metrics.md) for how those concepts are read now.

Two things follow. An extraction bug is a **report-integrity** bug, not only a
metric bug, so a fix to a normalisation chain means the reports resting on those
periods are stale and their claims must be re-checked rather than assumed. And
confidence in a claim can never exceed confidence in the pipeline that fed it —
which is why the deterministic layer carries the test suite it does, and why the
answer here is extraction tests rather than a second validation framework
checking the first one.

**8-K Item 2.02 is often boilerplate.** The substance of an earnings 8-K lives in
Exhibit 99.1, and exhibits are not fetched. What gets stored is the "furnished,
not filed" legend. Reports say so plainly rather than pretending otherwise.

**Re-extraction is manual.** Stored excerpts carry no parser version, so
improving the extractor does not invalidate anything automatically. Applying a
fix to already-read filings means running `research update-filing-text --force`
for the affected companies.

**Rankings remain `PRELIMINARY`** until the optional FMP enrichment pass verifies
market cap and consolidated liquidity. This is Phase 2 behaviour carried forward:
a `PRELIMINARY` ranking is a valid ranking, and research explains it as such —
the confidence ceiling accounts for it.
