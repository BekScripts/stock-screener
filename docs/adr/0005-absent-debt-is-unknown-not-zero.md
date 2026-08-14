# 0005. Absent debt is unknown, never zero

- Status: Accepted
- Date: 2026-08-14

## Context

XBRL has no single `totalDebt` concept. A filing tags the individual instruments
it has — long-term borrowings, the current portion, convertible notes, finance
leases — and tags nothing for the instruments it does not have. A company that
repays its debt simply stops tagging it: Palantir's most recent debt fact is a
zero filed in 2021, Culp's is from 2020.

That makes two very different situations indistinguishable from the outside:

- the company genuinely has no borrowings;
- the company has borrowings under a tag this adapter does not recognise.

An earlier version resolved this by reading absence on a filed balance sheet as
zero, on the grounds that it recovered a net-cash figure for the debt-free growth
companies the screener is built to find.

## Decision

Absence is `None`. Only a recognised value produces a number, and an explicit
zero in a filing is a recognised value.

`net_cash` is therefore `None` whenever debt is unknown, and Phase 2 must
distinguish `0` from `None` rather than treating both as "no debt".

## Alternatives considered

**Infer zero from a filed balance sheet.** This was implemented and then
reversed. It gives Palantir its $9.4bn of net cash, which is correct — but it
gives the same answer to a leveraged company whose borrowing tag is missing from
the chain, and that error flatters exactly the companies a risk penalty exists to
catch. A risky company appearing financially strong is worse than a strong one
appearing unmeasured.

**Corroborate with a second provider.** Attractive in principle: accept zero when
an independent source agrees. Rejected for now because the only other source
configured is FMP, whose profile endpoint carries no debt at all and whose
statement endpoints are gated for most symbols — so the corroborating branch
would almost never fire. It is worth revisiting if a source with broad balance-
sheet coverage is added.

**Broaden the tag chain until absence is trustworthy.** Partly done — the chain
covers borrowings and finance leases, and deliberately excludes operating-lease
payment schedules, which are neither borrowings nor balance-sheet amounts. But no
chain can be proven exhaustive against every filer, so breadth reduces the error
rate without justifying the inference.

## Consequences

Companies with genuinely no debt lose their net-cash figure and score `None`
rather than favourably. In the six-company smoke test that is Palantir, Amprius
and Culp — a real loss of signal on real companies.

In exchange, no company can be credited as debt-free because of a parsing gap.

Phase 2 must treat a missing net-cash or debt figure as unscored rather than as
neutral or as a maximum, in the same way missing dilution is handled. Giving an
unknown the benefit of the doubt would reintroduce this decision's failure mode
one layer higher.

Adding a newly encountered borrowing tag to the chain in `edgar.py` is the
maintenance this decision implies, and it directly improves coverage.
