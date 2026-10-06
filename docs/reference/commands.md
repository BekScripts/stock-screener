---
type: reference
title: Commands
---

# Commands

Every `make` target. Each wraps `uv run`, so the workspace environment is always
used.

| Target | Runs |
| --- | --- |
| `make setup` | `uv sync --all-packages --all-groups`, links agent skills, creates `.env` |
| `make sync` | `uv sync --all-packages --all-groups` |
| `make check` | `lint`, `format-check`, `types`, `test` |
| `make check-web` | In `frontend/`: `npm run typecheck`, `npm run lint`, `npm run build` |
| `make dev` | The API and the dashboard together; Ctrl-C stops both |
| `make lint` | `ruff check .` |
| `make format` | `ruff format .` then `ruff check --fix .` |
| `make format-check` | `ruff format --check .` |
| `make types` | `mypy` over `src`, `tests`, and any packages/scripts present |
| `make test` | `pytest --cov --cov-report=term-missing` |
| `make test-unit` | `pytest -m unit -q` |
| `make migrate` | `alembic upgrade head` |
| `make migrate-down` | `alembic downgrade -1` |
| `make scan` | `stock-screener run-scan` |
| `make cov` | `pytest --cov --cov-report=html` |
| `make docs` | `mkdocs serve` |
| `make docs-build` | `mkdocs build --strict` |
| `make sync-skills` | Symlinks `.agents/skills/` into `.claude/skills/` |
| `make clean` | Removes caches and build artifacts |

`make check` is what CI runs. Every Python check in CI is reachable through it.

`make check-web` is the frontend equivalent and is not a prerequisite of
`make check`: the Python gate runs without a node toolchain installed. It
requires Node and an `npm install` in `frontend/`.

## CLI commands

Installed as `stock-screener`; also reachable as `python -m stock_screener`.

### Ingestion and screening

| Command | Does |
| --- | --- |
| `update-universe` | Refreshes the company list from the market-data provider. |
| `update-market` | Refreshes daily OHLCV for stored companies, resuming from the last stored session.  `--since` backfills from a date instead of resuming from each company's last stored session, which is the only way to deepen existing history. |
| `update-eligibility-volume` | Refreshes consolidated average volume for the liquidity screen, from the consolidated tape rather than the price feed. Writes no price history. Run it before `enrich`: it is unmetered, so a company failing the liquidity gate here costs no provider request. |
| `update-benchmark` | Refreshes the benchmark's price history. Relative strength cannot be calculated without it. |
| `update-fundamentals` | Refreshes profiles and quarterly statements, skipping companies already current. |
| `scan` | Screens stored data and prints the eligible companies. Calculates no score. |
| `run-scan` | The universe, market and fundamentals updates followed by `scan`. |

### Scoring and rankings

| Command | Does |
| --- | --- |
| `score` | Scores every stored company and saves one snapshot per company for the day. Reads only stored data; calls no provider. Produces `PRELIMINARY` rows. |
| `enrich` | Spends one metered request per top candidate to verify market cap and consolidated volume, then re-scores them as `FINAL`. Stops when the quota runs out. |
| `rankings` | Prints Top Opportunities from the most recent scoring run. |
| `hidden-gems` | Small, fast-growing companies that already score well. |
| `wrong-price` | Great Company, Wrong Price — strong businesses whose valuation score is poor. |
| `improving` | The companies whose score has risen most over the window. |
| `explain` | One company's latest score, metric by metric. Takes a ticker. |
| `run-daily` | The whole nightly job: every update, then `score`, then `enrich`, then the ranking. |

### Point-in-time capture (Phase 8B)

Records what the system knew, on the day it knew it. Neither command calls a
provider for market data, and neither changes a score.

| Command | Does |
| --- | --- |
| `capture` | Copies the current scores and computes the observed market state, writing both as immutable rows for the day. Re-running a completed day writes nothing. A failure rolls the day back whole and records why. `--date` sets the day; it defaults to today in UTC, matching `score`. |
| `update-observations` | Appends what each filing reported, keeping every earlier version with the date it was filed. Costs no extra request straight after `update-fundamentals`, which leaves the filing document cached. |
| `update-acceptance` | Records when the SEC accepted each filing, which is what makes a point-in-time availability test correct. Skips companies already resolved, so a repeat run is nearly free. |
| `derive-signals` | Derives market signals from the captured features — peer percentiles, a multi-label setup, and a tradability verdict. No provider call, no ranking, no score. See [Market signals](market-signals.md). |

The distinction these two exist to preserve is between the operational tables,
which are overwritten as better information arrives, and the point-in-time
record, which is not. `score_snapshots` replaces the day's rows when `score` runs
again; `pit_score_snapshots` never does. `financial_snapshots` overwrites a
restated quarter; `fundamental_observations` appends the restatement beside the
original. See [Project state](PROJECT_STATE.md) for the rules that govern them.

### AI research (Phase 3)

`candidates`, `brief` and `update-filings` are inspection and ingestion; only
`run` calls a model. Assembling a brief touches no provider at all — it reads the
database and nothing else.

A research run never fails a pipeline. A quota, a timeout or an outage becomes a
`FAILED` report for that company and the run continues, so scanning, scoring and
ranking never depend on a model being reachable.

| Command | Does |
| --- | --- |
| `research candidates` | Lists the companies a research run would spend a model call on: the top of the ranking, the biggest score movers, and the hidden gems, deduplicated and capped at 25. |
| `research brief <TICKER>` | Assembles one company's research brief and summarises the evidence it carries — facts, score items, quarters, filings, and everything unknown. |
| `research update-filings` | Refreshes the stored SEC filing index that briefs cite. Metadata only; no filing text is fetched. `--candidates` restricts the pass to the current research set. |
| `research run [TICKER]` | Generates AI research, validates it, and stores what validation accepted. With no ticker, researches every candidate. Reuses a stored report whenever the evidence, the scoring rules and the prompt are all unchanged. |

| Option | Applies to | Effect |
| --- | --- | --- |
| `--ticker` / `-t` | the update commands, `scan`, `score` | Restrict to a symbol. Repeatable. |
| `--limit` / `-n` | `update-market`, `update-eligibility-volume`, `update-fundamentals`, `score` | Process at most this many companies, in ticker order. Bounds a first run against a metered provider. |
| `--force` | `update-fundamentals` | Re-store every period even when the provider has nothing newer. The incremental skip compares reporting dates and cannot tell that the adapter changed, so this is what picks up a normalisation fix. |
| `--output` / `-o` | `scan`, `run-scan`, the ranking commands | Write the result to a CSV. |
| `--all` | `scan` | Show excluded companies and their reasons in the table. |
| `--limit` / `-n` | `scan`, `run-scan`, the ranking commands | Cap the printed rows. Rankings default to 50. |
| `--dry-run` | `score` | Calculate without saving, to inspect a formula change before it enters the score history. |
| `--limit` / `-n` | `enrich` | Candidates to enrich. Defaults to `FMP_ENRICHMENT_LIMIT`. |
| `--preview` | `score` | How many top-scoring companies to print after scoring. Default 10. |
| `--min-score` | `rankings` | Only companies at or above this final score. |
| `--window` | `improving` | Days to compare back over. Default 30. |
| `--min-change` | `improving` | Minimum improvement, in score points. |
| `--limit` / `-n` | `research candidates` | Cap the selection across every source. Default 25. |
| `--json` | `research brief` | Print the whole brief as JSON, exactly as a model would receive it. |
| `--ticker` / `-t`, `--limit` / `-n` | `research update-filings` | Restrict the pass, as for the other update commands. |
| `--candidates` | `research update-filings` | Restrict the pass to the current research candidates. A full-universe refresh costs thousands of requests for filings no brief will cite. |
| `--dry-run` | `research run` | Print the exact prompt and brief that would be sent, then stop. No model is called and nothing is stored. |
| `--force` | `research run` | Ignore a stored report and generate a new one. |
| `--prepare` | `research run` | Fetch this company's SEC filing index and text first, so filing-dependent sections have evidence to cite. One ticker only, and nothing is generated if the fetch fails. |

Every command is idempotent: a second run updates rows rather than duplicating
them, including `score`, whose rows are unique per company, day and formula
version. A per-ticker provider failure is logged and counted, and does not end
the run.

Exit codes: `0` on success, `1` when `explain` has no stored score for the
ticker or `research brief` has no scored company to build one from, `2` when a
selected provider is missing its credentials.

### SEC evidence, derived (B2.12–B2.13)

The `evidence` group's derived-data commands read the trusted V2 corpus and
frozen state and write only rebuildable output; neither contacts the SEC,
writes an artifact prefix, or touches a database. Both verify the frozen
databases they are told to (`--expect-database-sha PATH=SHA256`) before and
after, and stop on any drift.

| Command | Does |
| --- | --- |
| `evidence build-index` | Reads every trusted artifact the V2 manifest names — once, verifying its size and SHA-256 against the manifest before parsing — into `derived/sec_evidence_index_v2.db`, in bounded transactions with a ledger that makes an interrupted build resumable and refuses a different manifest. Requires `BASELINE_COMPLETE`. Mirrors the small build metadata on completion; never the index. `--prefetch` overlaps downloads only; `--stop-after` stops cleanly; `--fresh` discards an in-progress build. See [Storage layout](storage-layout.md#b212-the-derived-index). |
| `evidence regenerate-a3` | Loads a frozen inputs package through its own loader — bound to the manifest, index, fingerprints, resolution and, when named, the database — runs `plan_coverage_first` to exhaustion, validates every target against the package, runs again to prove determinism, writes the plan with the historical writer, compares the count and digest to the historical 61,941 / `53e3f0ca…`, and mirrors the plan files under a prefix named after the plan directory (`a3_r1` → `a3-r1/`). A package or plan directory carrying `SUPERSEDED.json` is refused. `--expect-forensic-rows` and `--expect-forensic-sha` classify the plan as `EXACT_FORENSIC_REPRODUCTION` or `FORENSIC_PRODUCTION_MISMATCH`; a mismatch stops the run before anything is authorized or mirrored. `--authorize` writes `authoritative_a3_plan.json` (`AUTHORITATIVE_V2_A3_PLAN`, `AUTHORIZED_FOR_EXPANSION`) with the supersession chain from `--supersedes-package-sha`, `--supersedes-plan-sha`, `--forensic-commit` and `--correction-commit`. Never reassembles inputs; never downloads. See [A3 planning inputs](a3-planning-inputs.md#the-authoritative-plan-b214r2). |
| `evidence expand-a3` | Executes the authorized Plan A3 into the bucket. Authority is `authoritative_a3_plan.json` beside the plan plus the exact digests given on the command line (`--expect-plan-sha`, `--expect-plan-rows`, `--expect-package-sha`, `--expect-record-sha`); a directory carrying `SUPERSEDED.json`, a record without `AUTHORIZED_FOR_EXPANSION`, or a plan hashing differently is refused. Re-proves the B2.11 marker locally and as mirrored (`--expect-baseline-marker-sha`, `--expect-baseline-manifest-sha`, `--expect-baseline-trusted`), resolves every target's document from the V2 index, cuts the plan into frozen batches (`--batch-size`, default 1,000) that are mirrored before any request, and runs each target through the V2 chain — SEC GET, B2 PUT, HEAD, body GET, manifest append only after the remote body verified — with the SEC side sequential and the bucket side bounded by `--workers`. Completed batches are skipped on a rerun; a trusted target costs one HEAD and no SEC request; an unrecorded object is adopted only when it holds the SEC's bytes. Every tenth batch the namespace is walked and `--expect-database-sha` re-verified. After the last batch every A3 artifact is downloaded and hashed again, the namespace reconciled, the final manifest snapshot and `a3_expansion_complete.json` mirrored under `a3-expansion-r1/`, and recovery proven from the bucket alone. `--stop-after-batches` stops cleanly. Requires `--live` and `SEC_STORAGE_BACKEND=b2`. Never plans; never refreshes a catalogue or the ticker map. See [A3 planning inputs](a3-planning-inputs.md#executing-the-plan-b215). |
| `evidence build-post-a3-index` | Builds the post-A3 derived index incrementally: re-proves the B2.12 index (`--expect-baseline-index-sha`, its metadata, marker, fingerprints and integrity), the final manifest (`--expect-manifest-sha`, `--expect-trusted`; its newest mirrored snapshot must be it) and the A3 completion marker (`--expect-a3-marker-sha`, local and mirrored); derives the delta from the final manifest and the baseline ledger and requires it to equal the authorized plan (`--expect-plan-sha`, `--expect-plan-rows`); copies the baseline index, proves the copy, and reads only the delta's artifacts — one GET each, length and digest checked before parsing — through the unchanged extraction, in bounded transactions, resumably and bound to every input. Finishes with validation, semantic fingerprints, the baseline invariance proof (the baseline's rows fingerprint as B2.12 recorded), an atomic promotion to `sec_evidence_index_v2_post_a3.db`, and the small metadata mirrored under `post-a3-derived/`. Never fetches SEC, never writes evidence, never rereads the baseline corpus. See [A3 planning inputs](a3-planning-inputs.md#the-post-a3-index-b216). |
| `evidence materialize-pit` | Measures what A3 resolved: binds to the post-A3 index (`--expect-index-sha`) and its completion marker, the final manifest, the corrected package's coverage rows (`--expect-package-sha`) and the authorized plan (`--expect-plan-sha`); runs the unchanged B2.13 assembly over the post-A3 index `--runs` times (identical fingerprints required) and lays every previously open month's new standing beside its frozen pre-A3 one; writes `post_a3_pit/` (month outcomes, target realization, gap transitions, identity, year and gap summaries, fingerprints, the summary and `pit_final_complete.json`) and mirrors the small files under `post-a3-derived/pit/`. `ResearchUniverseMonth` is read only; never plans, never fetches. See [A3 planning inputs](a3-planning-inputs.md#what-a3-resolved-b216). |
| `evidence audit-pit` | Audits what remains open after A3 and measures — never takes — the residual SEC opportunity. Binds to the post-A3 index (`--expect-index-sha`), the final manifest, the PIT completion marker (`--expect-pit-marker-sha`), the corrected package and the authorized plan; re-proves the B2.16 materialization from its fingerprints; runs the unchanged assembly over the post-A3 index and classifies every residual month by the assembly's own reason into one audit class and one sufficiency category; builds the A3 marginal curve as a prefix union in plan order; runs the unchanged planner over the post-A3 candidates as a `FORENSIC_ONLY` / `NOT_AUTHORIZED_FOR_ACQUISITION` counterfactual; writes `pit_audit/` and mirrors its small files under `pit-audit/`. `--runs` audits must fingerprint identically. No SEC request, no evidence write, no A4. See [A3 planning inputs](a3-planning-inputs.md#the-residual-audit-b217). |
| `evidence freeze-backtest-contract` | Freezes `PIT_RESEARCH_DATASET_V2` and writes `BACKTEST_DATA_CONTRACT_V1` under `<control>/contracts/backtest_v1/`. Re-hashes every binding (`--expect-database-sha`, `--expect-universe-sha`, `--expect-manifest-sha`, `--expect-index-sha`, `--expect-pit-marker-sha`, `--expect-audit-marker-sha`, `--expect-package-sha`, `--expect-plan-sha`, plus `--also-verify PATH=SHA256` pairs) before and after, fingerprints the canonical projections of every research table `--fingerprint-runs` times (all must agree), builds the contract twice and requires identical content, writes the files, proves the written contract loads under its own bindings and is refused under altered ones, and mirrors the files under `backtest-contract/`. Reads only. See [Backtest data contract](backtest-contract.md). |
| `backtest run-baselines` | Executes R1.0 over the frozen dataset: re-proves every binding (`--expect-database-sha`, `--expect-contract-sha`, `--expect-coverage-sha`, `--expect-outcomes-sha` and the six evidence digests), optionally recomputes every dataset projection (`--verify-fingerprints`), derives the decision calendar, freezes `research_split_v1.json` and the baseline registry, writes and hashes the preregistration, builds the monthly cross-sections (`--workers`), executes the preregistered baselines gross and under `SIMPLE_10BPS_PER_SIDE_V1`, evaluates the frozen labels, validates every run manifest, runs both audits, and mirrors the compact metadata. `--runs 2` executes the whole phase twice and refuses to report unless every semantic fingerprint is identical. There is no `--force`. Reads only; writes nothing to either database. See [R1 backtest harness](backtest-harness.md). |
| `backtest review-signal` | Interprets the R1.0 record and decides whether one hypothesis-driven experiment follows. Re-derives every R1.0 fingerprint before computing anything (`--expect-r1-0-marker-sha`, `--expect-split-sha`, `--expect-preregistration-sha`, plus the database and contract digests) and refuses on any mismatch; recomputes the decile diagnostics by split, year, horizon and liquidity bucket, both as `D10-D1` and as `D10 - cross-section mean` with the profile's shape; runs leave-one-month-out and leave-one-year-out; measures rank persistence, profit concentration by identity and by month, holding concentration and the turnover decomposition; computes the analytic per-side cost break-even from the trade ledger; audits the funnel, the undated-classification exposure and the score floor; applies the fixed rubric; and writes the hypothesis record and, only if it survives, the frozen R1.2 specification — which it never executes. `--runs 2` requires identical fingerprints. Reads only. See [R1.1 signal review](signal-review.md). |
| `backtest run-quarterly` | Executes the preregistered `COMPOUNDER_QUARTERLY_TOP_DECILE_V1`. Asserts every rule it is about to apply against the stored R1.1 specification (`--expect-spec-sha`, `--expect-hypothesis-sha`, `--expect-r1-1-marker-sha`) and re-derives the whole R1.0 record beneath it (`--expect-r1-0-marker-sha`, `--expect-split-sha`, `--expect-preregistration-sha`) before computing anything; refuses a spec that tests more than one frequency, fraction, weighting, execution rule or cost level, or that carries a sweep. Slices the quarterly cross-sections out of R1.0's frozen ledger, simulates gross and under `SIMPLE_10BPS_PER_SIDE_V1`, compares against R1.0's own monthly ledgers sliced to identical sessions, evaluates the five frozen falsification criteria mechanically, and writes the hypothesis evaluation. `--runs 2` requires identical fingerprints. Reads only. See [R1.2 quarterly experiment](quarterly-experiment.md). |
| `backtest review-turnover` | Explains why R1.2 found C3 unmet, and tests nothing. Re-derives the R1.0 ledgers and every R1.2 artifact before reading a value (`--expect-contract-sha`, `--expect-database-sha`), refuses unless R1.2's interpretation is `PARTIALLY_SUPPORTED_BY_R1_2` with C3 the only unmet criterion, and refuses if any simulator is importable from the forensic package. Rebuilds annual turnover from the trade and holdings ledgers alone, documents the formula off the implementation, reconciles the two annualisation conventions in the frozen record, decomposes traded notional into entrants, exits, resizing and terminal legs, attributes every membership change to ranking or to an availability change, measures retention against the monthly survival curve at one, two and three months, bands entrants and exits by rank percentile, and computes the least trading the executed selections could have required. Chooses no parameter and simulates no strategy. `--runs 2` requires identical fingerprints. Reads only. See [R1.3 turnover forensics](turnover-forensics.md). |
| `backtest preregister-h2` | Preregisters `H2_MEMBERSHIP_STABILITY` and runs no experiment. Verifies the contract and database digests, re-reads R1.3's mechanism measurements and refuses if any has moved, and refuses if any simulator or pricing path is importable from the package. Writes the hypothesis record and the R1.5 specification with one rule (`ONE_DECILE_HYSTERESIS_V1` — ordinary top-decile entry, retention inside two selection widths), four criteria, and the aggregate rule `C1 AND C2 AND C3 AND C4` fixed in advance; records the frozen history as hypothesis-generation data that can never confirm H2. Computes no turnover and no return. `--runs 2` requires identical fingerprints. Reads only. See [R1.4 H2 preregistration](membership-hypothesis.md). |
| `backtest run-h2` | Executes the single preregistered H2 experiment. Verifies the whole upstream record and calls `verify_h2` before reading a price (`--expect-h2-sha`, `--expect-spec-sha`, plus the database and contract digests), then builds the decisions from the comparator's own frozen cross-sections and target counts, freezes the memberships, simulates gross and at 10 bps per side, gathers the accounting findings, rebuilds turnover under the frozen definition, and fixes the mechanism verdict — all before any return is computed. A durable execution marker refuses a second run. The result carries `EXPLORATORY_HISTORICAL_TEST` and confirms nothing. `--runs 2` requires identical fingerprints. Reads only. See [R1.5 H2 experiment](hysteresis-experiment.md). |
| `backtest freeze-h2-candidate` | Freezes `H2_MEMBERSHIP_STABILITY_CANDIDATE_V1` on R1.5's preregistered mechanism result and defines `H2_PROSPECTIVE_SHADOW_PROTOCOL_V1`. Verifies the closed historical record (`--expect-contract-sha`, `--expect-database-sha`) and refuses if any simulator, data client or broker is importable from the package. Writes the candidate freeze, the prospective data contract, the protocol and its three schemas: a four-transition window that cannot move, paired directional criteria that never use the historical effect size as a threshold, and a first eligible decision expressed as a rule rather than a date. Observes nothing, authorises no broker or orders, and leaves the runner inactive. `--runs 2` requires identical fingerprints. Reads only. See [R1.6 H2 candidate and shadow protocol](prospective-protocol.md). |
| `prospective activate` / `arm` / `status` | The H2 prospective shadow runner. `activate` writes one immutable activation record at the runtime clock's instant — a supplied timestamp, a dirty tree and a non-commit code reference are each refused — after verifying the R1.6 candidate, protocol and contract by digest, checking the frozen session calendar reproduces every historical benchmark session, and confirming no order-submission capability exists in the package. `arm` records arming durably and derives the first eligible decision from the protocol: the first quarter-end strictly after activation for which the runner was armed by the preceding session's close; a missed quarter is skipped, never reconstructed. `status` is read-only and reports the criteria as sealed. Observes nothing, trades nothing. See [R1.7 prospective shadow runner](prospective-runner.md). |
| `prospective build-source-plane` / `readiness` / `preflight` | The prospective source plane. `build-source-plane` audits the frozen V1 protocol for missed-observation handling, records the disposition, writes a successor protocol when V1 is silent, then exercises every adapter against live providers and assesses six-of-six readiness — creating no observation, and running the canary at its own as-of rather than the missed instant. `readiness` is the pre-event gate; `preflight` is the decision-day check meant to run before the instant. Late retrieval is permitted, late information is not. See [R1.9 prospective source plane](prospective-source.md). |
| `prospective certify-window` | Certifies the next eligible H2 window and, with `--arm`, re-arms under V2. Compares V2 against immutable V1 on the twelve fields H2's science rests on and refuses if any moved; classifies every source by what it can say about an earlier moment, so a current-only snapshot taken after the instant is refused as `CURRENT_ONLY_SOURCE_TOO_LATE`; derives the window and its execution-reference session from the calendar; freezes the staged capture sequence; and proves readiness and timing against live providers. Creates no H2 decision. See [R1.10 V2 window certification](prospective-window.md). |
| `prospective pipeline-canary` / `capture-readiness` | The capture pipeline R1.11 found missing. `pipeline-canary` runs the production fetchers, score-input builder, `COMPOUNDER_V1_2` scorer and ranker against live sources at its own as-of and reports six real, non-null fingerprints; it stops before H2 selection so it can never consume a prospective observation. `capture-readiness` reports `EXECUTABLE_CAPTURE_READINESS`, which distinguishes a reachable source from an implemented producer and from an existing executor — a derived component is `BLOCKED` until a producer has emitted and validated a fingerprinted artifact, and "derivable once its sources are admissible" is not readiness. See [R1.12 prospective capture pipeline](prospective-capture.md). |
| `prospective seal-predecision` / `capture-decision` / `capture-execution-reference` | The three capture stages. `seal-predecision` retrieves and seals the current-only universe and tradability before the decision instant, because the broker asset list cannot be asked later what it said at the close. `capture-decision` completes the package after the close with session-bounded prices and acceptance-bounded SEC evidence and filing observations, requires all six fingerprints before the packet is constructed, applies `ONE_DECILE_HYSTERESIS_V1` through its one authoritative implementation, writes exactly one decision packet, and advances the state only after the packet reads back as written. `capture-execution-reference` records the open of the derived execution session against that decision. A missed session is refused before anything is computed; no broker order is ever placed. See [R1.12 prospective capture pipeline](prospective-capture.md). |
| `portfolio evaluate` / `show-policy` | The portfolio and risk engine. `evaluate` takes a JSON request of strategy intents, a portfolio snapshot and a risk policy, and reports what would be authorised: approved targets, a decision and reason code per requested identity, inert execution intents, and a manifest binding every fingerprint. It reaches no broker, submits no order and holds no credentials — the package imports no HTTP client, socket or database, and there is no `--force`. `show-policy` prints the example policy, which is `NOT_PRODUCTION_AUTHORIZED`. See [P1.0 portfolio and risk engine](portfolio-risk-engine.md). |
| `books replay` / `verify-store` / `show-manifest` | Portfolio accounting. `replay` takes a JSON request of ledger events plus optional marks and an external statement, and reports the books they imply: the verified hash chain, cash and its decomposition, cost basis, NAV, the P&L split, per-strategy attribution and any reconciliation breaks. It derives state from events and changes nothing — it appends no event, simulates no fill, and the accounting core imports no HTTP client, socket or database. `verify-store` checks a stored ledger for tamper, deletion, reordering and insertion, and reports chain integrity and tail completeness separately. `verify-anchor` compares the durable head anchor against the stored ledger — the check a hash chain cannot make on its own, since a ledger missing its last records is a valid shorter chain. `migrate-attribution` reconstructs per-attribution cost pools by replaying a V1 ledger's events, and refuses rather than estimating when the history is incomplete. `show-manifest` prints every policy version and the digest over them. See [P1.3 portfolio accounting](portfolio-accounting.md). |
| `execution paper-run` / `verify-journal` / `show-manifest` | Deterministic local paper execution. `paper-run` takes a JSON request of an opening book, execution intents, a sizing snapshot and the paper prices to fill against, and reports the orders it planned, what filled and the resulting accounting book. It reaches no broker live or paper, holds no credential, and introduces no randomness — the same request always produces the same result. `verify-journal` checks an execution journal for tamper, deletion, reordering, insertion and — against its mandatory head anchor — truncation. `show-manifest` prints every frozen policy, the order lifecycle and what the engine deliberately does not model. It is a correctness simulator, not a realistic fill simulator: no spread, slippage, impact or latency. See [P2.0 paper execution engine](execution-engine.md). |
| `execution paper-run-realism` / `show-realism-manifest` | V1.1 deterministic execution with friction. `paper-run-realism` crosses the quoted spread, applies the declared adverse slippage, caps fills at a share-volume participation ceiling shared across each session, expires DAY orders at every close and rolls residuals into new calendar-derived child orders — reporting spread cost, slippage cost and implementation shortfall, which it posts to nothing because the book already holds the prices paid. `show-realism-manifest` prints the friction policy and what is still not modelled. No value in the policy is calibrated from any strategy's returns, and market impact, latency and queue position remain unmodelled. See [the execution engine](execution-engine.md). |
| `evidence build-a3-inputs` | Assembles Plan A3's inputs — PIT coverage, class-aware reachability, gaps, candidates, the held set and per-form statistics — from the frozen PIT database, the frozen resolution, the V2 index and the V2 manifest; writes them as a digested package, assembles a second time to prove it deterministic, round-trips it, and mirrors it. Candidates are catalogue filings dated inside the research window (`HISTORICAL_9L_V1`); `--supersedes-*` records the package this one replaces and leaves a `SUPERSEDED.json` beside it. Never runs the planner. Requires `DERIVED_INDEX_COMPLETE` and its mirrored marker. See [A3 planning inputs](a3-planning-inputs.md). |

## Scripts

Standalone tools, run with `uv` and not part of the deployable.

| Script | Purpose |
| --- | --- |
| `scripts/verify_fundamentals.py` | Checks a fundamentals provider's live response against the fields, units and sign conventions the adapter assumes. Read-only. Exits 0 when everything is present and plausible, 1 on a mapping problem, 2 when the provider is unreachable. |

```bash
uv run scripts/verify_fundamentals.py --ticker AAPL --expected-market-cap 3.4e12
```

## Test markers

| Marker | Selects |
| --- | --- |
| `unit` | Pure logic; no network, database, or filesystem |
| `integration` | Touches a real dependency |
| `slow` | Takes over a second |
| `live_b2` | Makes real Backblaze requests; skipped unless `--live-b2` |
| `live_paper` | Submits to a real **paper** broker; skipped unless `--live-paper` |

```bash
uv run pytest -m unit
uv run pytest -m "not slow"
```

### Opt-in suites

Two suites reach an external service and are skipped by default. `make check`
passes neither flag, so the ordinary gate makes no external request and submits
no order.

| Flag | Runs | Effect |
| --- | --- | --- |
| `--live-b2` | `tests/live/test_b2_canary_live.py` | Real Backblaze reads and writes |
| `--live-paper` | `tests/live/test_paper_broker_live.py` | Reads the configured **paper** account; one test submits one share |

The one command that reaches a broker is separate from the test suites:

```bash
stock-screener execution external-paper-preflight --confirm
```

It is read-only — every request is a `GET`, and it reports its own mutating
request count as zero so a reader can check rather than trust. It refuses
without `--confirm`, refuses without `ALPACA_PAPER_KEY_ID`,
`ALPACA_PAPER_SECRET_KEY` and `ALPACA_PAPER_ACCOUNT_ID`, and cannot be pointed
at a live host: the transport will not construct against one. There is no
corresponding command that submits an order.

```bash
uv run pytest tests/live/test_paper_broker_live.py --live-paper
```

Needs `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` and
`ALPACA_PAPER_ACCOUNT_ID`; the tests skip rather than fail when they are absent.

There is no live-trading equivalent of `--live-paper`, and adding one would
achieve nothing: the adapter refuses a live host, a live key prefix and a live
environment by construction, and no flag reaches those checks. The paper
adapter's own behaviour is fully covered in `make check` against an injected
fake transport.
