# Compounder Radar — dashboard

A small Next.js surface over the API. Four rankings, a company page, a
watchlist, and a control panel that runs the pipeline.

It calculates nothing. Every score, subscore, metric and claim on a screen came
out of the database through `stock_screener.api`, so the dashboard and the CLI
cannot disagree about what a company scored today.

It runs nothing either. Pressing **Run** posts a job; the API spawns the same
CLI command you would have typed, and the page polls until it finishes. No
pipeline logic lives here.

## Running it

From the repository root, one command for both:

```bash
make dev
```

Or separately — the API first:

```bash
uv run uvicorn stock_screener.api:app --reload    # http://localhost:8000
```

Then the dashboard:

```bash
cd frontend
npm install
npm run dev                                        # http://localhost:3000
```

`NEXT_PUBLIC_API_URL` points at the API and defaults to `http://localhost:8000`.
Copy `.env.example` to `.env.local` to change it.

## Checks

```bash
make check-web      # all three, from the repository root
```

or individually, from here:

```bash
npm run typecheck   # tsc --noEmit
npm run lint        # eslint
npm run build       # production build
```

These are deliberately **not** part of `make check`: the Python gate runs in CI
without a node toolchain, and making it depend on one would mean every backend
change waits for an npm install.

## Layout

```
app/
  page.tsx                  the four ranking tabs, and CSV export
  stocks/[ticker]/page.tsx  overview, score breakdown, metrics, research
  watchlist/page.tsx        watched companies
  jobs/page.tsx             the control panel
  loading.tsx  error.tsx    the loading and error states for every route
components/
  ranking-table.tsx         one ranking view
  research.tsx              the validated report, claim by claim
  watch-button.tsx          the watchlist star
  research-button.tsx       asks for AI research on one company
  run-button.tsx            starts one pipeline command
  job-console.tsx           the controls, the history and the log tail
  job-strip.tsx             what is running, on every page
  ticker-search.tsx         reaches a company the rankings do not show
  badges.tsx                score, category, risk and ranking-state badges
lib/
  api.ts                    every call to the API, and the types it returns
  format.ts                 formatting — including "missing renders as —, never 0"
```

## Polling

Nothing streams. The status strip and the job console ask every two seconds
while something is in flight and stop when nothing is, so an idle dashboard
makes one request per navigation rather than one every two seconds forever.

When a run finishes the route refreshes, which is how the rankings pick up a
completed scoring run without anyone reloading the page.

## Known limitations

**An unknown ticker can answer `200`.** `/stocks/<unknown>` calls `notFound()`
and renders the correct "No such company" UI, but the HTTP status may still be
`200` rather than `404`, because the App Router has already begun streaming the
layout by the time the page resolves. The status line is wrong; nothing a reader
sees is. Fixing it would mean resolving the ticker before the layout renders —
an extra round trip on every company page to correct a status code no person
reads. Not worth it for a dashboard with one user, so it stays documented rather
than fixed.

## What it deliberately does not do

No authentication, no alerts, no charts, no portfolio, no price targets, no
deployment config. A dashboard for one person reading a shortlist does not need
them, and each would need its own answer to "what happens when the data is
missing".

**No authentication is the one worth restating**, now that the Jobs tab can
spend money. The API binds localhost, which is the only thing making that
reasonable — see `JOBS_ENABLED` in the configuration reference.
