---
type: reference
title: Phase 4 — dashboard and watchlist
---

# Phase 4 — dashboard and watchlist

Phase 4 puts a screen in front of the pipeline. Four ranking views, a company
page, the grounded research report, and a watchlist — the one thing in the
product a person decides rather than the pipeline.

The rule the phase is built around: **the dashboard calculates nothing.** Every
score, subscore, metric and claim on a screen came out of a stored snapshot
through `stock_screener.api`. Scoring and ranking arithmetic stays in the
backend, so the dashboard and the CLI cannot disagree about what a company
scored today.

## Layout

| Piece | Lives in |
| --- | --- |
| Read models — the shapes a screen needs | `src/stock_screener/dashboard.py` |
| HTTP surface | `src/stock_screener/api.py` |
| Watchlist table and repository | `packages/data-access`, migration `0011` |
| The dashboard itself | `frontend/` — Next.js App Router |

`frontend/` is the second deployable in this repository, and the only approved
exception to the one-deployable rule. See the layout section of `AGENTS.md`.

## Endpoints

Added in this phase:

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/api/stocks/{ticker}` | Overview, score breakdown, metrics, watched flag |
| `GET` | `/api/stocks/{ticker}/research` | The latest validated research report |
| `GET` | `/api/watchlist` | Watched companies with their current scores |
| `POST` | `/api/watchlist/{ticker}` | Adds a company — idempotent, `201` |
| `DELETE` | `/api/watchlist/{ticker}` | Removes a company |

The Phase 1 and 2 endpoints — `/api/companies`, `/api/scan`, the four
`/api/rankings/*` views and `/api/companies/{ticker}/score` — are unchanged.

CORS is configured for `localhost:3000` and `127.0.0.1:3000` only. Origins are
listed rather than wildcarded because this API now has a write endpoint.

## Routes

| Route | Shows |
| --- | --- |
| `/` | The four ranking tabs |
| `/stocks/[ticker]` | Overview, score breakdown, metrics, research |
| `/watchlist` | Watched companies |

## What the screens guarantee

**Missing renders as missing.** `frontend/lib/format.ts` maps `null` and
`undefined` to an em dash for every unit — percent, money, points, score and
change. A metric the data cannot support never renders as `0`, and a company
without a score shows no number rather than a zero.

**Only validated reports are shown.** `research_view` reads a stored
`ResearchReport` — the type only validation constructs. There is no path from
the API to a raw model draft, and the dashboard has no endpoint that would
return one.

**A status is shown, not hidden.** An ineligible security, a bank and a company
with too little history each render the status that says why, rather than a
missing or invented score. Ranking state is rendered as `PRELIMINARY` or
`FINAL`, carried forward unchanged from Phase 2.

**The watchlist persists.** One row per company, uniqueness enforced by the
database rather than the caller, so adding twice is idempotent rather than a
duplicate. It survives restarts, rescans and rescores; it holds a ticker, a
timestamp and an optional note, and nothing else.

## Known limitations

Documented rather than fixed. None blocks normal use.

**An unknown ticker can answer `200`.** `/stocks/<unknown>` calls `notFound()`
and renders the correct not-found UI, but the HTTP status may still be `200`,
because the App Router has already begun streaming the layout by the time the
page resolves. The rendered page is correct; only the status line is wrong.
Fixing it would mean resolving the ticker before the layout renders — an extra
round trip on every company page to correct a status code no reader sees.

**One watchlist, no users.** The `watchlist` table has no owner column. The
dashboard is a single-person tool with no authentication, so every client that
can reach the API shares one list.

**Local origins only.** The CORS allowlist names the two development origins.
Serving the dashboard from anywhere else means changing that list — there is no
deployment configuration in this phase, deliberately.

**The frontend is outside `make check`.** `make check-web` runs the TypeScript
typecheck, ESLint and the production build, and is not a prerequisite of
`make check`: the Python gate runs in CI without a node toolchain. A frontend
regression is caught by running `check-web`, not by the Python suite.

**Rankings remain `PRELIMINARY`** until the optional FMP enrichment pass
verifies market cap and consolidated liquidity. Phase 2 behaviour carried
forward; the dashboard renders the state rather than hiding it.

## Out of scope

Not built, and not a backlog: alerts, notifications, authentication, deployment
configuration, charts, portfolios, positions and price targets. Each would need
its own answer to "what happens when the data is missing", and a dashboard for
one person reading a shortlist does not need them.
