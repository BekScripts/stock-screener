---
type: reference
title: Phase 5 — run control
---

# Phase 5 — run control

Phase 4 could read everything and start nothing, so using the dashboard meant a
terminal open beside the browser and nine command names to remember. Phase 5
adds the other half: the pipeline runs from the interface.

The rule the phase is built around: **a job is an existing command, never new
pipeline code.** `JOB_KINDS` maps a key to a CLI argument list, and that mapping
is the whole implementation. Adding a stage means adding a CLI command first.

## Jobs are subprocesses

The API spawns `python -m stock_screener <args>` and records a row. It does not
run pipeline code in-process.

A market-wide run takes the better part of an hour. FastAPI serves a `def`
endpoint from a threadpool, so doing that work in the handler would occupy a
worker for the duration, and an ingest that died would take the API with it. A
child process keeps the API answering, isolates a crash, and captures output to
a file.

**A job carries no result.** Every command already persists what it produces, so
a finished research job means `/api/stocks/{ticker}/research` now has something.
The record answers "is it running, did it work", and nothing else.

## What can be run

| Kind | Command | Spends money |
| --- | --- | --- |
| `daily` | `run-daily` | no |
| `scan` | `scan` | no |
| `score` | `score` | no |
| `enrich` | `enrich` | **yes** — metered FMP requests |
| `research` | `research run --prepare <ticker>` | **yes** — a model call |
| `update-universe` | `update-universe` | no |
| `update-market` | `update-market` | no |
| `update-benchmark` | `update-benchmark` | no |
| `update-fundamentals` | `update-fundamentals` | no |

A kind absent from this table cannot be run. The caller picks a key, never an
argument, and a `research` target is matched against a ticker pattern before it
can reach a command line.

**Research is per-company only.** There is no batch trigger and the daily run
does not research anything. Selection already caps a candidate run at 25
companies with a `RESEARCH_MAX_RUN_COST_USD` ceiling; a button that spends money
is pressed one company at a time deliberately.

## Statuses

| Status | Means |
| --- | --- |
| `RUNNING` | A live process is behind it |
| `SUCCEEDED` | Exited zero |
| `FAILED` | Exited non-zero — the log says why |
| `UNKNOWN` | The process is gone and was never closed |

`UNKNOWN` is distinct from `FAILED` on purpose. An API restart mid-run leaves it
behind, and the command may well have completed — calling that a failure would
assert something nobody observed.

## Concurrency

One running pipeline job at a time, and one research job per ticker. Every
kind except `research` writes the shared tables, so any of them blocks any
other: `scan` and `score` are different commands over the same rows, and
`update-fundamentals` beside `score` means scoring a market that is changing
underneath it. A second request returns `409` with the job already in flight
rather than starting a competitor.

`research` is outside that guard. It reads a snapshot that is already stored and
writes only its own company's report, so it neither blocks a pipeline run nor
waits for one, and research on one company has no reason to block research on
another.

## Also in this phase

**Ticker search.** Ranking endpoints cap at 500 rows over a universe of
thousands, so most scored companies could not be reached in the interface at
all. The masthead search matches ticker or name, exact ticker first. A company
that is not ranked is still reachable, and its page says which status kept it
out.

**CSV export.** Each ranking view exports the rows it shows. Built by the API
rather than the page, for the same reason everything else is: a file assembled
client-side could disagree with the screen it came from. A missing metric is an
empty cell, never `0`.

## Running both processes

```bash
make dev
```

Starts the API and the dashboard together and stops both on Ctrl-C. `API_PORT`
and `WEB_PORT` override the defaults.

A busy port stops the run rather than producing a Node stack trace, and the
message names the invocation holding it — walking up from the socket, because a
reloading uvicorn answers from a fork child whose command line is a bare
interpreter path.

```bash
make dev FORCE=1
```

Reclaims the ports, but only from this application: it looks for our own uvicorn
invocation in the holder's ancestry and refuses anything it does not recognise.
Port 8000 is a port half the tools on a laptop want, and killing whatever answers
there would eventually kill something else. Note that stopping the API does not
stop a job it spawned — jobs are detached, so the work continues and its row
reconciles to `UNKNOWN`.

## Security

**The job endpoints spend money and have no authentication.** They are only
reasonable because uvicorn binds `127.0.0.1` by default. Anything that can reach
the API can start an hour-long ingest or a billed model call.

`JOBS_ENABLED=false` removes the whole surface — every job endpoint answers
`404`, so a disabled feature looks absent rather than guarded. Set it before
binding the API to anything but localhost.

## Known limitations

Documented rather than fixed.

**A restart orphans a running job.** The child keeps going — it is detached —
but its exit code went with the process that spawned it, so the row becomes
`UNKNOWN` rather than resolving. The log file is still written and still
readable.

**Progress is a log tail, not a percentage.** The interface polls the last lines
a command wrote. Commands report stage summaries rather than completion
fractions, so "34% done" is not available without the pipeline emitting it.

**Job history grows without bound.** Rows accumulate so that "when did this last
run" stays answerable. At one run a day this is negligible; there is no
retention policy because nothing yet needs one.

**Log files are never cleaned up.** One file per job under `JOB_LOG_DIR`. A
market-wide run writes several megabytes.

**Polling, not streaming.** The interface asks every two seconds while something
is in flight and stops when nothing is. Server-sent events would be tidier and
would add a long-lived connection to a single-user tool that does not need one.

## Deliberately absent

Not oversights, and not a backlog. Each is a thing this phase decided against,
recorded so it is not re-argued later.

**No scheduler.** Nothing here runs a command at a time of day. A daily run is a
person pressing Run Daily, or `run-daily` from a crontab the operating system
owns — which is one line, already works, and does not need the application to
grow a second way of starting things.

**No retries.** A failed job stays failed and says so. Commands are idempotent,
so the fix for a transient provider failure is pressing the button again, and a
retry that ran on its own would spend money on a schedule nobody watched.

**No queue and no workers.** One mutating job runs at a time and a second
request is told what is already going. A queue would exist to hold work that has
nowhere to run, and on one machine with one database there is no such work.

**No arbitrary flags.** `JOB_KINDS` maps a key to a fixed argument list. The
maintenance commands take `--ticker`, `--limit` and `--force` from a terminal
and none of them from the interface: the moment a caller can supply an argument,
the allowlist stops being one.
