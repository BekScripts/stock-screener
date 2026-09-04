---
title: Historical listing evidence
type: reference
---

# Historical listing evidence

Where the project's knowledge of **which venue a security traded on, at a past
date** comes from, and — more to the point — where it stops.

## The source

Nasdaq publishes a monthly *Market Share by Symbol* file per venue family at
`https://www.nasdaqtrader.com/content/marketstatistics/marketshare/`. Phase 9K
audited every free alternative and found this the only usable historical record
of listing venue.

| Family | Tape | Venue | Covered here |
| --- | --- | --- | --- |
| `NASDAQ{YYYYMM}.xlsx` | C | NASDAQ | 2022-01 onward |
| `Listed{YYYYMM}.xlsx` | A | NYSE | 2022-01 onward |
| `Listed{YYYYMM}.xlsx` | B | Arca / American / regional, pooled | **never imported** |

Files are read with the standard library — an `.xlsx` is a zip of XML — so the
workspace carries no Excel dependency.

## The family changed meaning

`Listed*` is **Tape B** through 2021-12 and **Tape A** from 2022-01. The 2017-01
file names `SPY` and not `JPM`; the 2022-01 file names `JPM` and not `SPY`. One
month inside the earlier era — 2021-07 — is Tape A anyway.

Reading the family name at face value would therefore relabel five years of NYSE
Arca ETFs as NYSE listings. `domain.tape_for` predicts a tape from the month,
`domain.classify_tape` reads one out of the file's own contents, and the importer
accepts only a file where the two agree. A disagreement becomes a `QUARANTINED`
row in `listing_source_files` carrying both tapes and the reason.

## Evidence states

| State | Means |
| --- | --- |
| `MARKET_SHARE_OBSERVED` | The month's file for that venue named the symbol. **Month resolution.** |
| `INFERRED_INTERVAL` | A day inside an observed month, or continuity across an unbroken run of months. |
| `UNKNOWN` | No observation — a hole, anything outside the observed run, or a security the source cannot see. |
| `CENSORED_VENUE` | NYSE American, which appears in no importable file. |

There is deliberately **no `NOT_LISTED` state**. The source cannot produce that
claim: a security that did not trade in a month is simply absent, and 34.6% of
observed symbols have at least one hole inside their own run. Absence is
`UNKNOWN`, and the type system is what prevents a caller writing anything else.

## Monthly is not daily

`domain.month_state` may return `MARKET_SHARE_OBSERVED`. `domain.daily_state`
**never** does — it returns `INFERRED_INTERVAL` for a day inside an observed
month, because the file attests to a month and a security that left on the 5th
appears in it exactly as one that traded through the 31st. The two functions are
separate rather than one call with a flag so that a caller has to choose the
granularity it is willing to defend.

A missing month between two observed months is `UNKNOWN` and is never filled.
That is what keeps Bed Bath & Beyond's Nasdaq run (2022-01..2023-05) from being
joined to the unrelated issuer that took the symbol on NYSE in 2025-08.

## Identity

An observation is keyed by `(symbol, venue, observation_month)` — what the file
actually establishes. Identity is a *resolved attribution* carried beside it, via
the Phase 9J rules, and `identity_state` records which of `RESOLVED_COMPANY`,
`RESOLVED_HISTORICAL`, `REUSED`, `AMBIGUOUS` or `UNRESOLVED` applies.

`listing_observations` is therefore the one dual-identity table that does **not**
carry the Option C "exactly one" check. Both identity columns may be NULL
together, because an unattributed observation is the ordinary case for a source
that gives only a ticker. What is enforced is *never both*.

## Known limitations

- **Monthly only.** No free daily listing source exists at any date.
- **NYSE American is censored.** 247 live companies are stored on it and the
  source sees essentially none of them. They are counted, never assumed absent.
- **A market-share file is not an authoritative listing roster.** Presence
  conflates listing with trading at least once that month.
- **No security class.** The file names warrants and units alongside common
  stock and offers no field to separate them; class comes from SEC cover pages.
- **Nothing before 2022-01** is imported, and the pre-2016-10 files are a legacy
  binary `.xls` format this project does not read.
