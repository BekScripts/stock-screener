---
type: reference
title: Project state
---

# Project state

The single checkpoint of what Compounder Radar *is* right now: which phase is
current, which decisions are frozen, what the schema and score definitions are,
and what must not be changed without being asked.

This page is a **state record, not a plan**. It describes what exists. Intent
lives in the phase briefs; rationale lives in the ADRs; procedure lives in the
skills.

## How this page is maintained

Update it **in the same commit** as any of the following. A change to one of
these that leaves this page stale is an incomplete change.

| Trigger | What to update |
| --- | --- |
| A phase completes or is frozen | Current phase, Frozen decisions, Next phase |
| A new `score_version` | Score definitions, Regression requirements |
| A new migration | Database migrations, Data contracts |
| A new frozen rule or ADR | Frozen decisions, Things explicitly not to change |
| A limitation found and accepted | Known limitations |
| A validated rollout or measurement | Completed experiments |
| Data promoted to the primary database | Completed experiments, Latest checkpoint |
| Any merge to `main` | Latest checkpoint |

Everything asserted here must be checkable against the repository. No number
appears without the run or the test that produced it.

---

## Current phase

**P2.4's first soak is HALTED at 0 of 20 and the programme is blocked on a
missing upstream authority**, on `feat/p2-execution-engine`. The protocol froze
and armed correctly; the first eligible session then exposed that nothing can
run it. P2.3 defined `OperationsEnvironment` as an injected protocol and no
concrete implementation was ever built, so the coordinator has no production
environment to gate.

The deeper cause is not the missing adapter. **There is no strategy authority in
this repository.** The only place a `StrategyIntent` is constructed in
production code is `portfolio_risk.run.load_request`, deserialising a JSON file
an operator writes. Nothing derives an intent from the universe, the scores, the
signals or the research record. Seven of the eight authorities an operational
run needs exist and are production-capable; the first link does not, and it
cannot be invented as integration plumbing.

**P2.1 is complete and frozen** at `p2-execution-realism-complete` (`be0db072`),
which freezes `EXECUTION_ENGINE_V1_1` and
`PAPER_SESSION_PARTICIPATION_FILL_V1`. P2.2 adds a second *venue* rather than
changing any local arithmetic: the 339 P2.0 and P2.1 tests pass unchanged and
`EXECUTION_MANIFEST_V1_1` still reports `external_paper_broker:
NOT_IMPLEMENTED`, because a V1.1 engine has no adapter.

**P2.0 is complete and frozen** at `p2-execution-foundation-complete`
(`e4ad0c8a`), which freezes `EXECUTION_ENGINE_V1` and
`PAPER_REFERENCE_PRICE_FILL_V1`. P2.1 adds friction on top rather than changing
any of it: the V1 goldens reproduce byte for byte and the V1 manifest keeps
describing the V1 lifecycle and vocabulary.

**Live trading remains `NOT_AUTHORIZED`, and from P2.2 that is structural.**
There is no flag, host, key prefix or configuration value that enables it.

**P2.2C adds the concrete paper transport.** `AlpacaPaperHttpTransport`
(`ALPACA_PAPER_HTTPS_TRANSPORT_V1`) is the one real `BrokerTransport`: host-locked
to `paper-api.alpaca.markets` in its constructor, `trust_env=False` so no
environment variable can re-route a submission, redirects refused, and no retry
loop anywhere in the module. Execution semantics are unchanged —
`EXECUTION_ENGINE_V1_2` is not bumped, because a concrete implementation of an
abstract seam moves no figure.

**P2.2D closed the four certification defects.** `read_fills` now filters by the
provider `order_id` and validates every returned row against the order it was
asked about; `cancel_order` cancels by the provider UUID, records a durable
intent, and proves the outcome by reading the order back rather than trusting a
`204`; `PROVIDER_CONFIRMS_PAPER` is backed by
`PROVIDER_PAPER_ENVIRONMENT_PROOF_V1`, which cannot be built from configuration;
and the preflight reads each endpoint once, binding every reported figure to the
response whose fingerprint sits beside it. Working orders and order history are
now separate concepts.

**The real read-only preflight PASSES.** Against the configured paper account:
origin `paper-api.alpaca.markets` reached, credential accepted, the configured
account matched its binding, paper proof evidence-backed, **3 HTTP requests (all
GET), 0 mutating requests** — where P2.2C made 6 for the same information. The
account identifier is deliberately not recorded here: it lives in `.env` and
reaches artefacts only as a salted fingerprint.

**P2.2 EXTERNAL PAPER EXECUTION IS CERTIFIED.** On 2026-10-07 one authorised
order went to the configured Alpaca paper account and back, through the full
chain, and the venue finished where it started.

| | |
| --- | --- |
| provider / environment | ALPACA / PAPER, `paper-api.alpaca.markets` |
| entry | BUY 0.1 F, confirmed, filled 0.1 @ 12.078 |
| cleanup | SELL 0.1 F, confirmed, filled 0.1 @ 12.062 |
| client-order-id lookup | matched the submitted order on both legs |
| accounting events | one per fill, derived from the provider execution id |
| duplicate re-poll | admitted 0; ledger length, cash and position unchanged |
| three-way reconciliation | `RECONCILED` after fill and after cleanup |
| residual position | 0 — closed by a compensating authorised order |
| certification fingerprint | `805169ff94055d8afda41d33435664818665ecabec10a106a186bb4126212cfa` |
| live trading | `NOT_IMPLEMENTED`, `NOT_AUTHORIZED` |

The artefact is `P2_2_EXTERNAL_PAPER_CERTIFICATION_V1`. It is not committed —
it names provider order and activity ids, which are operational references
rather than repository content — and its fingerprint is bound here instead.

Three things the live run taught that 318 offline tests had not: the venue
reports an order `filled` before its FILL activity is queryable, so a single
activities read after a terminal status returns nothing and must not be read as
"no fill"; a deterministic client order id makes a canary non-repeatable by
design, which is every layer behaving correctly and is resolved with a
run-scoped plan id rather than a random one; and fractionability is a property
of the instrument, so the capability is declared from the asset read instead of
from a venue-level default.

**P2.3 adds the paper operations control plane.** `PAPER_OPERATIONS_ENGINE_V1`
in `src/stock_screener/paper_operations/` is the layer that decides whether the
system may act at all, and most of it is refusal: arming permits a coordinator
and never an order, absent or corrupt control state reads as `KILLED` rather
than as permission, permission is re-read before every order instead of cached,
a run id is derived from the slot so one scheduled decision cannot become two
orders, a missed slot is recorded and never run late, an unreachable venue
blocks rather than falling back to the simulator, and no reconciliation break is
ever repaired automatically. It is versioned apart from `EXECUTION_ENGINE_V1_2`
and `PORTFOLIO_ACCOUNTING_V2`, neither of which moves because a coordinator
exists. Checkpoint: tag `p2-paper-operations-complete` (`68709d22`).

**P2.4 arms a preregistered multi-day paper shadow soak.**
`PAPER_SHADOW_SOAK_PROTOCOL_V1` adds no trading capability and no authority — it
is a preregistration, a recorder and a scorer, and its point is the distinction
between a control plane that passes tests and one that has operated.

| | |
| --- | --- |
| protocol | `PAPER_SHADOW_SOAK_PROTOCOL_V1`, frozen at arming |
| required evidence | 20 eligible sessions, derived from the calendar |
| declared calendar | `US_EQUITY_DECLARED_2026Q4_2027Q1`, 76 forward sessions |
| scoring | `evaluate_soak` takes no threshold arguments |
| zero-tolerance gates | 10, each producing `FAIL_SAFETY_VIOLATION` |
| non-breach gates | 5, each blocking an `A` without being a violation |
| performance gates | **none** — no gate reads a return, price or slippage |
| required drills | 6, all performed, all passed, all injected faults |
| live trading | `NOT_IMPLEMENTED`, `NOT_AUTHORIZED` |

Three properties carry the design. The protocol is **prospective**:
`SoakStore.qualifies` refuses a record for a session that closed before
activation, because twenty retrospective sessions chosen after the fact say
nothing about the twenty-first. The **goalposts cannot move**: every threshold
lives inside the protocol body so the fingerprint changes if one does, and
`evaluate_soak` has nowhere to accept a kinder limit — there is no pass-rate
field anywhere in the report. And **evidence appends**: an observation id is
content-addressed over the protocol, session, run id and the observation's own
fingerprint, so an identical re-record is idempotent while a correction gets its
own file and cannot silently replace the original.

`FAIL` and not-`A` are deliberately different verdicts. A duplicate order, a
duplicate accounting event, a trade on stale data or a backfilled run is a
safety violation. An eligible session with no record, or a control plane that
correctly refused to trade, blocks promotion without being a breach — reporting
either as a violation would be wrong on the facts and would also be the noise
that gets real violations ignored.

The six drills are `KILL_RESTART`, `DUPLICATE_SCHEDULER`, `CRASH_RECOVERY`,
`PROVIDER_UNAVAILABLE`, `REMOTE_STATE_UNKNOWN` and `STALE_INPUT`. Every one is
an injected fault against a stub environment over a throwaway root, and that is
a decision rather than a shortcut: the two that matter most are an unreachable
provider and an unresolved submission, and arranging either against a real venue
means interrupting a real order — an experiment that can itself cause the
duplicate it tests for. Each record carries `injected_fault: true`, because an
injected outage and a real one are different evidence.

**P1 is complete and frozen** at `p1-portfolio-authority-complete`
(`b87e7c96`), which closes the portfolio programme at P1.4. From that tag the
portfolio layer is the sole authority over holdings, cash and P&L, and nothing
above it may impersonate that authority. `feat/p1-portfolio-risk-engine` remains
frozen at the same commit; P2 branched from it.

| Programme | State |
| --- | --- |
| Data / PIT foundation | **COMPLETE** |
| H2 prospective track | **CAPTURE_READY_AND_ARMED — FROZEN UNTIL OPERATIONAL CHECKS** |
| P1 portfolio / risk | **ACTIVE** |
| P1.0 | **FOUNDATION COMPLETE** — `PORTFOLIO_RISK_ENGINE_V1` |
| P1.1 | **ADVANCED SINGLE-PORTFOLIO RISK COMPLETE** — `PORTFOLIO_RISK_ENGINE_V1_1` |
| P1.2 | **MULTI-STRATEGY CAPITAL / CORRELATION / RISK BUDGETS** — `PORTFOLIO_RISK_ENGINE_V1_2` |
| P1.3 | **ACCOUNTING AUTHORITY COMPLETE** — `PORTFOLIO_ACCOUNTING_V1` |
| P1.4 | **ATTRIBUTION / DURABILITY HARDENING** — `PORTFOLIO_ACCOUNTING_V2` |
| P1 | **PORTFOLIO AUTHORITY COMPLETE** — tag `p1-portfolio-authority-complete` |
| P2 execution | **ACTIVE** |
| P2.0 | **BROKER-NEUTRAL ORDER LIFECYCLE / LOCAL PAPER EXECUTION** — `EXECUTION_ENGINE_V1` |
| P2.0 checkpoint | tag `p2-execution-foundation-complete` |
| P2.1 | **DETERMINISTIC EXECUTION REALISM / MULTI-SESSION FILLS** — `EXECUTION_ENGINE_V1_1` |
| P2.1 checkpoint | tag `p2-execution-realism-complete` |
| P2.2 | **EXTERNAL PAPER ADAPTER / THREE-WAY RECONCILIATION** — `EXECUTION_ENGINE_V1_2` |
| P2.2C | **CONCRETE PAPER TRANSPORT PRESENT** — `ALPACA_PAPER_HTTPS_TRANSPORT_V1` |
| P2.2C real read-only preflight | **PASSED** — paper host reached, account matched, 0 mutations |
| P2.2D | **CERTIFICATION DEFECTS CLOSED** — fills, cancel, paper proof, single-read evidence |
| P2.2E | **EXTERNAL PAPER CANARY EXECUTED** — one authorised order, filled, reconciled, closed |
| P2.2 external paper endpoint | **EXTERNAL PAPER EXECUTION CERTIFIED** — ALPACA PAPER |
| P2.2 checkpoint | tag `p2-external-paper-certified` |
| P2.3 | **PAPER OPERATIONS CONTROL PLANE** — `PAPER_OPERATIONS_ENGINE_V1` |
| P2.3 checkpoint | tag `p2-paper-operations-complete` (`68709d22`) |
| P2.3 default operational state | **NOT_ARMED / FAIL CLOSED** |
| P2.3 kill switch | **PERSISTENT** — survives restart; corrupt state fails closed |
| P2.3 auto liquidation | **NONE** |
| P2.3 automatic reconciliation repair | **NONE** |
| P2.3 provider outage fallback | **NONE** |
| P2.3 scheduled-run backfill | **NONE** |
| P2.4 | **PAPER SHADOW SOAK ARMED** — `PAPER_SHADOW_SOAK_PROTOCOL_V1` |
| P2.4 soak protocol | **FROZEN AT ARMING** — cannot be changed or re-activated |
| P2.4 retrospective observations | **REFUSED** — `SOAK_RETROSPECTIVE_OBSERVATION` |
| P2.4 threshold arguments to the scorer | **NONE** — every limit comes from the protocol |
| P2.4 pass-rate field | **NONE** — 19 of 20 clean is a failed soak |
| P2.4 performance gates | **NONE** |
| P2.4 observation overwrite | **REFUSED** — append-only; corrections supersede |
| P2.4 verdict at arming | **B — SOAK ARMED, PROSPECTIVE OBSERVATIONS PENDING** |
| P2.4 checkpoint | tag `p2-paper-shadow-soak-armed` |
| P2.4 first activation | **HALTED_BEFORE_FIRST_OBSERVATION** at 0 of 20, permanently |
| P2.4 halt reason | `CONCRETE_OPERATIONS_ENVIRONMENT_MISSING` |
| P2.4 halt classification | `PROGRAM_INTEGRATION_GAP` |
| P2.4 V1 protocol `4b50f8b4…` | **SUPERSEDED** — never resumable, never reusable |
| Strategy authority | **DOES NOT EXIST** — intents are an operator-authored input |
| H2 broker-paper orders | **NOT AUTHORISED** — its own frozen ladder says so |
| Live trading | **NOT IMPLEMENTED, NOT AUTHORIZED** |
| Production risk thresholds | **NOT AUTHORIZED** |
| Production strategy budgets | **NOT AUTHORIZED** |
| Production correlation thresholds | **NOT AUTHORIZED** |
| Broker authority | **PAPER READ/SUBMIT, CERTIFIED — NO LIVE AUTHORITY** |
| Live trading | **NOT AUTHORIZED** |

**The H2 branch is semantically frozen.** Its arm binds `code_commit` to
`703cf847e550c21bf133d307281183ba3c9d8f24`, so a further commit on that branch
would break the binding and the December capture would refuse with `COMMIT_DRIFT`.
P1 therefore develops on its own branch and requires no edit to the armed one;
`h2-prospective-capture-v2-armed` is the immutable local checkpoint.

### Authority is layered, and the layers are permanent

| Layer | Authority | May not |
| --- | --- | --- |
| Strategy | signal: what is worth holding | size a position, or reach execution |
| Portfolio engine | allocation: what would express the signal | overrule a limit |
| Risk engine | veto and resize: what is permitted | be bypassed by a strategy |
| Execution engine *(later)* | order planning | choose what to hold |
| Broker adapter *(later)* | external action | decide anything |

A strategy may request; it may not insist. `portfolio_risk.evaluate` is the only
path from intents to targets, and no function in the package accepts a force,
bypass or override argument — asserted by a test walking every signature, because
the rule is worth exactly what it is worth on the day someone wants it not to hold.
Only the future broker adapter will hold credentials or submit an order; every
layer above it is structurally incapable of reaching one.

### P2.1 — what execution costs, and what DAY actually means

`EXECUTION_ENGINE_V1_1` adds friction to P2.0's lifecycle and corrects one thing
P2.0 got wrong. V1 remains frozen and reproducible; V1.1 is selected by supplying
a policy, a calendar and session snapshots.

**The friction is declared, never calibrated.** Slippage, participation and the
session horizon are inputs on a fingerprinted `PaperExecutionPolicy`, and nothing
in the package reads a return, a price history or a P&L figure to choose one. That
restraint is the point: a cost model fitted to make a backtest match an observed
result becomes a free parameter of the strategy, and the strategy's apparent edge
then partly comes from the assumption chosen to flatter it. Every example value is
`NOT_PRODUCTION_AUTHORIZED`.

**Spread and slippage are strictly adverse.** A buy crosses to the ask and is
worsened from there; a sell crosses to the bid and is worsened from there. A
negative slippage is refused by name — a model that could improve a fill is a
model that can manufacture edge. A crossed quote is `INVALID_PAPER_QUOTE` rather
than normalised, and a missing quote side is `PAPER_QUOTE_REQUIRED` with **no
fallback to a sizing price, close or last trade**: substituting one price for a
quote silently removes the spread cost this model exists to charge.

**Participation is global to `(identity, session)` and derived from durable
fills.** The ceiling is `observed_share_volume * participation`, in *shares* — not
P1's average dollar volume, which would misprice it by a factor of the price —
and it is shared across every child touching that security that session rather
than granted per order. Consumption is read back out of the journal keyed by fill
id, so a replayed fill consumes capacity once; a counter would double-consume on
every retry, and retries are the normal consequence of a crash.

**DAY now means DAY.** P2.0 let a DAY order live indefinitely, which was simply
wrong. A session close with quantity outstanding now produces `EXPIRED`,
terminally, kept distinct from `CANCELLED` because a cancellation is a decision
and an expiry is a lifetime ending — conflating them would make an unattended
rollover indistinguishable from an operator pulling an order.

**Residual rolls as new child orders, never by reviving an expired one.** Each
child's id is a digest over the plan, the security, the session, the generation
and the authorised residual, which makes creation idempotent across a crash and a
fabricated child impossible to pass. Children are authorised by *derivation*
rather than list membership, because a plan cannot enumerate children whose count
depends on fills that have not happened. Residual comes from admitted fills, not
submitted quantity — a child sent for 400 that filled 250 leaves 750 of a 1,000
parent, not 600. The next session comes from an explicit `ExecutionCalendar`:
Friday rolls to Monday and a pre-holiday residual to the day after, and `date + 1`
would have placed a child on a day the market was shut.

**The horizon is finite.** An exhausted session budget leaves the plan
`PARTIALLY_COMPLETED`, which is a real outcome. Unbounded rollover would keep
trading against a portfolio decision made arbitrarily long ago.

**Cost is decomposed exactly and posted to nothing.** `shortfall == spread +
slippage` is enforced on every report, not asserted in prose. The book already
received the actual fill price and the actual fee, so the cost is *already* inside
cash, cost basis and realised P&L — posting a spread or slippage expense
alongside would charge it twice and make NAV wrong by exactly the amount being
reported. The reports explain the prices the book holds; they do not adjust them.

**One timing semantic.** A snapshot is a completed session whose execution
opportunity is its own `as_of`; bid, ask, volume and fill all belong to that
instant and no fill is backdated inside the session. Using a whole session's
volume while dating the fill earlier would spend information the fill could not
have had — the same leakage the research layer exists to prevent.

Two structural problems surfaced while building this. The V1 manifest began
reporting V1.1's lifecycle and codes, so the manifest and the transition table are
now versioned and each reports what *that* engine can actually do — a V1 order
genuinely cannot expire. And `require_authorizes` could not see a child order at
all, because children are derived rather than listed; `require_authorizes_child`
recomputes the deterministic id, which is a stronger check than membership would
have been.

**Still not modelled**, and stated in the manifest: market impact, latency, queue
position, hidden liquidity, venue routing, auction behaviour and intraday price
paths. Impact in particular is a function of order size against liquidity, and a
flat declared haircut is not an approximation of it.

### P2.2 — a real venue, and why none of it can trade live

`EXECUTION_ENGINE_V1_2` adds the first code in this project that can cause
something to happen outside the process. Every decision in it is shaped around
containing that, and six are worth recording because the permissive alternative
is in each case one line long.

**Live trading is refused by construction, not by configuration.**
`BrokerEnvironment` can name `LIVE` because refusing something requires being
able to say it, and `is_authorized` is false for it everywhere; `refuse_live`
runs when a binding is built, when an adapter is constructed and when an order is
submitted. There is no `USE_LIVE`, no `--live`, no `mode=live` and no
`fallback_to_live`. Three independent checks must agree — the requested
environment is `PAPER`, the host is on the paper allow-list, the credential id
carries a paper prefix — and an **unrecognised** host or prefix is refused rather
than assumed safe, because the cost of guessing wrong is asymmetric: too strict
stops a run, too lenient places a real order. Only the key *id* is ever
inspected; nothing is logged.

**Configuration is not proof.** A proof from configuration alone is `is_proven`;
it becomes `is_fully_certified` only when the account answers a read-only
request. The two are different claims about different things and are kept apart,
which is what lets a preflight honestly report that it has not yet called.

**An order submission is never retried.** A `POST` that times out may have
created an order, so the fix is not resending but naming: the client order id is
*derived* from the local order id
(`DERIVED_CLIENT_ORDER_ID_SHA256_V1`, no UUID and no clock), the intent is
written durably **before** the request, exactly one mutating request is made, and
an ambiguous answer is resolved by *asking* the venue about that name. The id
deliberately does not depend on the account binding — a binding fingerprint moves
when a capability declaration changes, and an id that moved with it would let the
same order be sent again under a new name after a configuration edit.

**An unknown stays unknown.** `REMOTE_STATE_UNKNOWN` is never downgraded to
`NOT_SUBMITTED` for tidiness; the intent stays unresolved on disk, preflight
withholds readiness while one exists, and a person settles it. Planning new
orders while an unresolved intent exists would size them against a book that may
be missing a position.

**A stale display ticker is never a routing fallback.** Identity-to-symbol is an
explicit declared mapping, fingerprinted into the manifest. `SecurityIdentity.symbol`
holds whatever the ticker was when that row was loaded, and a ticker is reassigned
when a company delists — so routing on it would eventually send a real order for
company A to whatever company now answers to A's old ticker. An unmapped identity
is `BROKER_SYMBOL_MAPPING_UNRESOLVED` even when its display ticker is a symbol
this run *did* map. Two identities sharing a venue symbol is refused in both
directions. The provider status map is closed the same way: an unknown status is
`BROKER_STATUS_UNMAPPED`, never defaulted, because a default branch would report
working orders as dead the day the provider adds a status.

**A reconciliation break is reported across three records and repaired in none.**
Journal, accounting book and venue are compared, because two records cannot
localise a disagreement. `BLOCKING` stops execution, `WARNING` does not threaten
the next decision, `INFO` records an expected difference. There is no function
that adopts the venue's figure and a test asserts its absence — adopting one
would not resolve the break but destroy the evidence of it, and if the broker
were the record in error it would write a false position into an append-only
ledger that carries it forever. The cash tolerance defaults to exactly zero;
widening it is a decision somebody records.

**One venue per session, with no fallback.** `ExecutionVenue` has exactly two
members and no `AUTO`. A run that fell back to the simulator when its venue was
unreachable would leave a journal where some fills were simulated and some real
with nothing saying which, and every figure over it would be a blend of two
incompatible things. A local session may not even hold a credentialed binding.

**What P2.2 does not establish.** It ships no HTTP transport: `BrokerTransport`
is the seam, the whole adapter is exercised against an injected fake, and
nothing here has spoken to a real venue. The paper endpoint is therefore
**implemented but not certified**. Separately, an external run is **not
reproducible** — its fills came from a venue at a moment that will not recur —
and the V1.2 manifest states that rather than implying otherwise. A provider's
paper engine is also the provider's model of itself: it may fill more readily
than its live engine would, it is not bound by this project's participation
ceiling, and a paper fill is evidence the plumbing works, not that the trade was
achievable.

**No account funding, transfer, close-all or liquidate call exists.** A one-call
flatten is unrecoverable when invoked by accident, and the ordinary path already
does it with every check applied. No credential, authorization header, token or
cookie is persisted or logged, and a report is scanned for anything shaped like a
secret on the way out.

### P2.2C — the transport, and three things the real API taught us

`ALPACA_PAPER_HTTPS_TRANSPORT_V1` is the first code here that opens a socket to
a broker. The engine version does **not** move: a concrete implementation of an
abstract seam changes no figure, and bumping it would invalidate artefacts to
record that nothing happened.

**The host lock lives in the constructor.** `approved_paper_origin` runs before
a credential is attached and before any socket exists, so a misconfigured
transport fails at build time rather than at submit time, and cannot be
re-pointed afterwards. It refuses a live or brokerage host by name, and refuses
as *not provably paper* a non-HTTPS scheme, userinfo in the URL, an explicit
non-443 port, an IP literal, `localhost`, any unknown host, and a request path
given as an absolute URL.

**`trust_env=False` is the least obvious line in the module.** With httpx's
default, `HTTPS_PROXY` or `ALL_PROXY` would route every request — order
submissions included — through whatever host that variable named, while the URL
still read `paper-api.alpaca.markets`. A host lock an environment variable can
tunnel out of is not a host lock.

**Two refusals happen before any I/O.** A payload JSON cannot represent (a
`Decimal`, a NaN) and an absolute-URL path both raise rather than classify,
because the request provably did not arrive. The first version of this module
had the encode inside the try block, which turned a local serialisation bug into
a *network ambiguity* — and an ambiguity strands a submission intent and blocks
the next run. Encoding above the try is the whole fix.

**Nothing classifies to a definite failure.** Every transport-level error maps to
an outcome whose `is_ambiguous` is true, so a timeout on a `POST` stays unknown
rather than becoming failed. Failure details name the exception's class, never
its message, which can carry the full request URL.

**Evidence excludes the request.** Each reply carries the provider's
`X-Request-ID` where present, an allow-list of safe response headers, and a
fingerprint over status, those headers and the normalised body. Request headers
are excluded by construction: the request carried the credential, and a digest
over it would be a credential derivative in an audit record forever.

Then the part worth recording most. **Contact with the documented API exposed
three defects in the P2.2 adapter**, none of which the fake-transport suite could
have found, because a fake answers whatever it is told to:

| Defect | Reality | Consequence |
| --- | --- | --- |
| `read_fills` sends `client_order_id` to `/v2/account/activities/FILL` | the documented filter is `order_id`, the provider UUID | the parameter is ignored; the read returns the account's other fills and stamps each with this order's client id. Defeats per-order exactly-once ingestion. **Blocks certification.** |
| `cancel_order` builds `DELETE /v2/orders/{client_order_id}` | the path parameter is the provider UUID | a cancel would 404. Fails closed, and cleanup uses a compensating order rather than a cancel, so it is off the certification path. |
| `PROVIDER_CONFIRMS_PAPER` checks `is_paper` | `GET /v2/account` has no such field; paper and live differ only by host | the gate passes by absence. Substantively sound — paper-ness rests on host lock, auth success and account match — but the name overstates the evidence. |

They are **recorded, not fixed.** Each needs a contract change, and bundling one
into a transport-certification commit would make the diff unreviewable as either
thing. The lesson is the one worth keeping: a fake transport proves the shape of
a protocol and says nothing about whether the protocol matches the provider.

### P2.3 — the control plane, which is almost entirely refusal

P2.2 proved one authorised external paper execution can be made safe. P2.3 is
what makes running it again, unattended, also safe — and the striking thing
about the package is how little of it does anything. Most of it declines.

**It gains no authority, structurally.** `PaperOperationsCoordinator` takes
planning and submission as an injected callable. It cannot invent a target, an
order, a fill, a position, a cash balance or a risk approval because it has no
code that could build one. That is a stronger guarantee than a convention, and
it is why the coordinator can be trusted to sit beside execution and accounting
without becoming a second authority over either.

**The default is no, and corruption is not permission.** A fresh install is
`NOT_ARMED`. A missing kill-switch file is `KILLED`; so is an unparseable one, a
tampered one, and one naming a state this build does not recognise. The
inversion is the whole design: any other default lets a disk error grant the
right to trade. Control-state writes are read back and compared before being
trusted, because for a kill switch "I wrote it" and "it is there" are different
claims and only the second survives a power loss.

**Arming permits a coordinator, never an order.** This distinction is the one
most tempting to lose, because an operations grant is the broadest permission in
the system. If arming could authorise an order then one operator action would
stand between a scheduled process and an arbitrary trade. Instead every order
still needs portfolio → risk → `ExecutionPlan` → `Order`, unchanged. A grant
names one account fingerprint — a wildcard would keep authorising after a
credential was repointed, exactly when it must stop — and `expires_at` has no
invented default, because a grant that silently expired would strand the
scheduler and one that never expires outlives its reason.

**A kill stops activity; it does not trade.** It blocks submissions and cancels
open orders it can *prove* it placed. It never liquidates a position: a filled
position is an accounting fact an authorised decision created, and only another
authorised decision may unwind it. A kill that flattened the book would be the
largest unreviewed trade the system ever made, at the exact moment something is
already known to be wrong. It never touches an order it cannot prove it owns —
that is somebody else's. And the state is written *before* the venue is
contacted, so a provider outage cannot stop the system becoming killed.

An owned order whose fate cannot be established yields
`KILLED_WITH_UNRESOLVED_REMOTE_ORDER`, strictly worse than `KILLED` and never
collapsed into it — there may be a working order nobody is tracking. Leaving a
kill requires a clean reconciliation, no open blocking incident and no
unresolved owned order; `RunState.KILLED` has no outgoing edge, so a restart is
not a recovery.

**Permission is not cached.** The kill switch is re-read before every single
order, not once per run, and the gate counts its own checks so a test can assert
it ran per submission. An operator who kills mid-run must not have to wait for
the run to finish.

**A run is identified, not generated.** The run id is a digest of the slot's
semantic inputs and carries no clock, so invoking the scheduler twice for one
slot recovers the first run instead of starting a competitor — a UUID would make
every invocation a new run, which is the easiest way to turn one scheduled
decision into two orders. A durable lease admits one mutation-capable run per
account, and a stale lease is never taken on age: a stale lease and a crashed run
are the same observation from outside, and the crashed run may already have
submitted.

**Nothing is substituted for missing truth.** A stale input blocks rather than
being traded on. A missing one blocks rather than being guessed or carried over
from the last run. An unreachable venue blocks rather than falling back to the
local deterministic broker — the venue is bound for the run. A missed slot is
recorded and never run late, because an order planned against yesterday's
prices is a new unreviewed decision wearing an old authorisation. A
reconciliation break blocks and is never repaired. A `REMOTE_STATE_UNKNOWN`
blocks the whole account rather than being isolated so other names keep trading.

**There is no `DEGRADED` path to a mutation.** Only `HEALTHY` permits a
submission. Every "degraded but probably fine" exception anyone has written is
the one that later turned out to have been the problem. A required check that
did not *run* fails the report rather than being assumed passed: a check that
never ran has established nothing.

**Severity is behaviour, not tone.** `CRITICAL_AUTO_KILL_REASONS` is frozen and
short — account mismatch, paper environment unproven, live trading not
authorised, ledger or journal integrity failure, duplicate accounting fill,
unverifiable control state. Each means the system has lost track of what is
true. Ordinary trouble blocks instead, because escalating routine problems to a
state needing operator recovery trains an operator to clear kills without
reading them. An unclassified code defaults to `BLOCKING`: the safe reading of
"I do not know what this is" is "do not trade".

The journal uses the same chain-plus-mandatory-anchor pattern as the execution
journal and the accounting ledger, with the same honest limit stated in its own
`verify()` output: **a coordinated local rollback of the journal and its anchor
together is undetectable from local files alone.**

### P2.2E — the canary, and three more things only a real venue could say

One authorised order, out and back. What it certifies is deliberately not that
HTTP works: it is that the authority chain holds. The order originated from an
accounting-V2 book, through a plan bound to that book's state, to the submission
protocol, to the transport. The canary has no path that calls the transport to
trade, because a canary that did would prove connectivity and nothing about what
keeps an unauthorised order from existing.

**The activities feed lags the order status.** The venue reported the order
`filled` about twenty milliseconds after submission and the matching FILL
activity was not yet queryable. The first run therefore read one empty
activities page and reported `ENTRY_NOT_FILLED` — while the venue had in fact
bought 0.1 F. The position was real and had to be closed. Executions are now
polled in their own bounded loop, and an exhausted window is *reported* rather
than concluded from, because "I did not see a fill" and "there was no fill" are
different facts and only one of them is safe to act on.

**A deterministic client order id makes a canary non-repeatable, and every layer
was right about it.** The second run derived the same venue-facing id; the venue
refused the duplicate; the lookup returned the first run's filled order; and
accounting refused that fill with `EVENT_TIME_REGRESSION` because it took effect
before the new book opened. Three independent refusals, all correct, and
together they say the plan id was wrong rather than that any of them was. The
fix is a run-scoped plan id derived from the run's own moment — stable *within* a
run, so a retry after a crash still derives the same client order id and stays
idempotent, and different between runs. Not a random id: randomness would have
bought repeatability by giving up the crash-safety the derivation exists for.

**Fractionability belongs to the instrument, not the venue.** The binding's
capability default is whole shares, which is right for a venue and necessarily
wrong for one side or the other of any particular asset. The canary asks the
venue whether the symbol is fractionable and declares the capability from that
answer, recording the asset-read fingerprint it relied on. A refusal to trade a
fraction of a non-fractionable asset remains, and nothing is silently rounded.

Two differences are declared rather than absorbed. The fee is a schedule value —
zero for a US equity at this venue — and the execution still records
`fee_is_known=False`, because the venue stated none; the declaration and the
observation stay distinguishable. And a one-cent cash tolerance is declared with
its reason: the venue rounds cash to whole cents, so 0.1 share at 12.062 is
exactly 1.2062 of proceeds reported as 1.21, and the 0.0038 is rounding rather
than a missing trade. The global default remains exactly zero, because a default
tolerance is how a drift becomes invisible.

What the certification does not establish is worth keeping in view. It is one
observation: this code, this provider, one moment. An external run is not
reproducible, and the artefact says so. And a provider's paper engine is the
provider's model of itself — it may fill more readily than its live engine
would, it is not bound by this project's participation ceiling, and its queue
behaviour is unobservable from here. A paper fill is evidence the plumbing
works, not evidence the trade was achievable.

### P2.2D — what a fake transport cannot tell you

Four defects, all of which passed 211 fake-transport tests. That is the lesson
worth keeping from this phase: a fake answers whatever it is told to, so it
confirms that a protocol has the shape the code expects and says nothing about
whether that shape matches the provider. Three were found by reading Alpaca's
documentation; the fourth by running the preflight once against a real account.

**`read_fills` filtered on a parameter the endpoint does not have.** It sent
`client_order_id` to `/v2/account/activities/FILL`, where the documented filter
is `order_id`. An unrecognised query parameter is ignored rather than rejected,
so the read returned the account's FILL activities broadly *while looking
successful*, and each row was stamped with the requested order's client id. On
an account with prior activity that posts unrelated trades into the book.

The repair is not a parameter swap. There is no fallback to the client id —
`REMOTE_PROVIDER_ORDER_ID_REQUIRED`, because the failure mode of a silently
ignored filter cannot be seen in the reply. The provider id comes from the
durable submission resolution, so it survives a restart. And since an ignored
filter is undetectable from the response, every row is now checked against the
order it was requested for: another order's execution, one naming no order, or
one in an unmapped symbol is `REMOTE_FILL_ORDER_MISMATCH` and is refused rather
than attributed. A row that does not positively declare itself a fill is
skipped, so a dividend or a fee on a mixed page cannot become an execution.

**`cancel_order` used the wrong identifier and believed the status code.** The
path parameter is the provider UUID, not the client id, so a cancel would have
404'd; no durable intent was written, so a crash during the request left nothing
naming the order; and a `204` was treated as terminal, which it is not — a
cancellation can be accepted and still lose a race to a fill. It now mirrors
submission exactly: durable intent, one `DELETE`, then the outcome established
by *reading the order back*. `REMOTE_STILL_OPEN` and `REMOTE_STATE_UNKNOWN` are
both possible and neither is a cancellation; only the first permits a further
deliberate attempt, because only it rests on proven state.

**`PROVIDER_CONFIRMS_PAPER` could not fail.** It checked `is_paper is False`,
and the real account object has no such field — paper and live differ only by
host. The gate passed by absence, reporting attestation nobody had gathered. A
gate that cannot fail is worse than no gate: it occupies the place where a check
should be, and reads in a report as though something was verified. It is now
backed by a proof that can only be constructed from a successful authenticated
response from the approved origin that matched this account binding. There is no
constructor taking configuration, and a response from a live origin raises
rather than proving anything.

**The preflight read each endpoint twice** — once to fingerprint, once for the
values — so the artefact's digests described responses it had not reported.
Neither read was wrong and the artefact was still worthless as evidence.
`ExternalReadEvidence[T]` now pairs a normalised value with the fingerprint of
the same response, and against the real account the run makes three requests
where it made six.

**And the counts were mislabelled.** `status=all` was reported as "orders at
venue": 50 rows of mostly-terminal history presented as live state. Working
orders and history are now separate queries and separate concepts, and
reconciliation cares about working orders — a terminal order from an earlier
session is history, not a break.

None of this moved `EXECUTION_ENGINE_V1_2`. These were implementation defects in
how the adapter talks to one provider, not changes to what a fill or an order
means. `V1_ERROR_CODES` stays at 40 and `V1_1_ERROR_CODES` at 50, so the frozen
P2.0 and P2.1 manifests do not move; the two new codes join the V1.2 set, which
no artefact had yet been produced under.

### P2.0 — an authorised target becomes a fill, and nothing else does

`EXECUTION_ENGINE_V1` is the first layer whose output could, once a real broker
adapter exists, move money. It therefore starts from two separate claims, both
load-bearing.

**An order is not a position.** Creating, submitting or acknowledging one changes
no holding, no cash and no NAV. Only an admitted accounting `FillEvent` changes
the book, and the paper broker holds no cash, positions or P&L of its own —
buying power *is* accounting's cash. A simulated broker with its own balances
would be two books that agree until they do not.

**A target is not an order.** An `ApprovedPortfolio` and an `ExecutionIntent` are
inert. Orders exist only inside an `ExecutionPlan`, which binds the targets, the
intents, the sizing snapshot, the valued snapshot and the book. There is no
`force`, no `unsafe_submit` and no arbitrary-order path, asserted by a test over
the broker's own signatures.

**`PORTFOLIO_ACCOUNTING_V2` is demanded, never inherited.** P1.4 left V1 as the
accounting default so existing ledgers kept their meaning; accepting that default
here would open new execution books under the pooled-basis convention P1.4 exists
to replace, and nobody would notice until two strategies entered one security at
different prices.

**The frozen baseline** is `MARKET`, `DAY`, long-only, positive `Decimal`
quantities to eight places. Anything else is refused by name rather than
approximated. `SELLS_BEFORE_BUYS_V1` sequences exits and reductions ahead of
entries, because a no-margin book cannot fund purchases until sales release cash;
within a group, durable identity decides. No strategy is prioritised.

**The sizing price is not a fill price.** Quantity is `|notional delta| / sizing
price` from an explicit snapshot; what the order fills at comes from the venue. A
market order with no execution price is `PAPER_EXECUTION_PRICE_MISSING` — the
sizing price is never substituted, because that fallback would make every
rebalance execute at exactly its planned level and the simulation could never
show a cost.

**The handoff is two facts, not one.** A fill is durable execution evidence
before the book is touched, and the handoff is confirmed only after the book is
durable. A crash between them leaves the fill `PENDING`, and recovery admits it
under an accounting event id *derived* from the fill id — so a second admission
is a no-op rather than a second trade. Crash recovery was verified in both
directions: fill-durable-without-accounting, and accounting-applied-without-
confirmation.

Two defects were found by building that path. The broker was appending to the
journal *and* the store was committing the same event, so every event looked like
a retry and the anchor never advanced past 1 — the broker is now a pure event
producer and durability belongs entirely to the store. And the plan digested each
order's authorisation fingerprint, which is the plan's own digest, making the two
mutually recursive; order *content* and the back-reference to the plan are now
separate.

**Insufficient cash is never overridden.** If fill prices move against a buy,
accounting refuses and execution fails closed. No overdraft, no fabricated cash,
no automatic resize — resizing would mean trading a quantity the risk engine
never approved.

**The journal applies P1.4's lessons from the start**: append-only, hash-chained,
with a mandatory head anchor written on every committed event rather than when
somebody remembers, and admissibility decided before any durable write. Order
state is derived by replay, never stored.

**Not guaranteed, and stated as such.** A journal and its local anchor rolled
back together validate cleanly; local files cannot establish that they are the
latest state of themselves. And the engine models no execution cost at all — no
spread, slippage, impact, participation or latency — so a strategy run through it
will look better than it would trade. Both appear in the manifest, because a
simulated return that does not disclose its cost assumptions invites being read
as a forecast.

**Live trading is NOT AUTHORIZED.** No live broker adapter, no external paper
brokerage, no credential anywhere in the package.

### P1.4 — exact per-strategy basis, and a ledger that knows how long it is

P1.3 closed with two stated limitations. P1.4 closes both, and states plainly what
is still not guaranteed.

**Strategy cost accounting: per-attribution weighted average.**
`PORTFOLIO_ACCOUNTING_V2` keeps one weighted-average pool per
`(identity, owner)` rather than one per security. It is a *major* version because
the change is not confined to attribution: for `A buys 1 @ 10, B buys 1 @ 20,
A sells 1 @ 30`, V1 reports global realised 15 and remaining basis 15 where the
truth is 20 and 20. The portfolio's own figures move, so V1 cannot be reproduced
from V2's rules and the additive `V1_1`/`V1_2` pattern does not apply.

V1 stays the default. A ledger or request declaring no version replays as V1, so
books written before P1.4 keep their meaning without a field they could not have
known to write; V2 is selected explicitly per ledger generation. The P1.3 goldens
reproduce byte for byte, which required scoping the V2 state fields and freezing
the V1 manifest's error-code list to the codes the V1 engine can actually raise —
a V1 manifest claiming a vocabulary its engine does not have would be false, and
would move a frozen fingerprint every time a later phase added a code.

**The pools are the single authority.** Global quantity, cost basis, realised
P&L, market value and unrealised P&L are all derived by summing the pools, never
maintained beside them. Four new identities are enforced exactly on every
valuation, and `INSUFFICIENT_ATTRIBUTED_POSITION` refuses a sale beyond one
owner's pool even when the security's global quantity is sufficient.

Two subtleties were resolved in favour of exactness. A pool's unrealised P&L is
`market value − total_cost_basis`, not `quantity × (mark − average_cost)`:
`average_cost` is derived at eight places while the basis is authoritative, so
the second form leaves a quantisation residue that accumulates across pools and
would surface as a false P&L decomposition failure. For the same reason global
market value is summed from the pools rather than computed as `quantity × mark`.

A pool that reaches zero keeps its realised P&L and its owner's name, closing the
second P1.3 limitation. It is never relabelled `UNATTRIBUTED_EXISTING`.

**Migration is `REPLAY_MIGRATION_FROM_EVENTS` or a refusal.** Fills carry price
and attribution, so replaying a complete V1 ledger from genesis reconstructs each
owner's pool deterministically. Migrating from a V1 *snapshot* is refused with
`ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE`: a V1 state holds one basis per security
and the owners' entry prices were destroyed at the first co-owned buy. An
incomplete or unverifiable history is refused the same way. There is no automatic
migration and the V1 ledger is never mutated.

**Ledger completeness: a mandatory local head anchor.** A hash chain proves its
records are consistent and cannot prove there were no more of them; P1.3's answer
was a checkpoint, which depended on somebody remembering to write one. The anchor
(`PORTFOLIO_LEDGER_HEAD_ANCHOR_V1`) is written on every committed event and binds
the generation, sequence, ledger head, state fingerprint and engine version.
`verify()` now reports chain integrity and tail completeness separately, because
a store written before anchors existed has the first and not the second.

Two defects were found by building the commit path and fixed structurally. A
semantically-invalid event was being written to disk *before* the state machine
refused it, which would have left a ledger that could never be replayed again —
admissibility is now decided before anything durable is written. And the state
fingerprint included the display symbol, which the ledger deliberately does not
persist, so an anchor could never match the same ledger read back from disk; the
digest now covers accounting content only.

**Coordinated local rollback detection: NOT GUARANTEED WITHOUT AN EXTERNAL TRUST
ANCHOR.** A ledger and its local anchor rolled back together to a mutually
consistent earlier state validate cleanly. Local files cannot establish that they
are the latest state of themselves. There is a test asserting this honestly
rather than one claiming otherwise. `ExternalAnchorSink` is the seam for a future
independent anchor — a protocol and an in-memory implementation, with no network
capability anywhere in the accounting package.

**Tax accounting: NOT IMPLEMENTED.** No FIFO, LIFO, specific-lot identification,
holding periods or wash-sale rules, asserted by a test over every class and
function name in the package.

**Broker authority: NONE.** Unchanged.

### P1.3 — what is actually owned, as distinct from what was authorised

`PORTFOLIO_ACCOUNTING_V1` adds the layer the risk engine could not provide: a
record of holdings. P1.0–P1.2 decide what a portfolio *should* contain, and none
of that is evidence that anything was bought. A target and a holding are now
separate objects with separate truth conditions.

**State is derived, never set.** A `PortfolioState` comes from replaying an
append-only ledger and from nowhere else — no setter, no constructor from a broker
statement, no patch. The hash chain (`PORTFOLIO_LEDGER_HASH_CHAIN_V1`) makes
tamper, deletion, reordering and insertion detectable from the records alone.
Truncation is the documented exception: a ledger with its tail removed is a valid
shorter chain, so checkpoints record a head hash and a count and
`CHECKPOINT_UNVERIFIABLE` catches it.

**Four identities are checked on every valuation**, exactly rather than within a
tolerance, because every figure entering them was quantised by one `Decimal`
policy:

```
cash  = opening + deposits - withdrawals - purchases + sales - fees + dividends
NAV   = cash + market value of holdings
P&L   = realised + unrealised + income - fees
NAV - inception NAV at cost = P&L + net external flows - unpriced cost basis
```

The last one is the strongest check in the package: it compares the P&L story
against the balance sheet rather than against itself. It found a real defect
during construction — realised P&L had been recorded net of fees, so a commission
paid on a *purchase* left cash and entered no P&L component at all, and a
portfolio that had only ever bought reported a NAV change wrong by exactly its
fees. Fees are now their own term and `realised` is gross of them, so a purchase
commission is charged exactly as a sale commission is.

**Refusal beats approximation.** A buy the cash cannot fund is `INSUFFICIENT_CASH`
(fees included in the test), a sale beyond the holding is `INSUFFICIENT_POSITION`
rather than a short, a mark stamped after the valuation instant is
`MARK_AFTER_VALUATION_TIME` — the same leakage the research layer prevents, which
here would simply be called a NAV — and a valuation missing any holding is
`VALUATION_INCOMPLETE`, because a NAV missing one position is wrong rather than
small. A stale mark is a reported warning, not a refusal.

**Only two corporate actions are modelled**, cash dividends and splits. Everything
else is refused by name with the terms it would have needed. A guessed cost basis
is indistinguishable from a correct one downstream, and an ignored action leaves
the ledger describing a security that no longer exists as recorded.

**Reconciliation never repairs.** There is no repair function and it cannot be
configured to have one — asserted by a test over the module's function names. A
difference from a custodian is evidence that something happened the ledger was not
told about; adopting the external figure destroys that evidence and leaves the
cause in place. Four break kinds are distinguished because they have different
causes, and an incomplete statement never reconciles cleanly.

**Decimal here, float there, one seam.** Accounting is exact; risk arithmetic is
statistical. `to_risk_snapshot` is the only crossing and it goes one way —
nothing writes a target into a holding.

Storage is one JSON file per record under `.portfolio/`, chosen over SQLite
deliberately: a database would also provide `UPDATE`. Writes are `fsync`ed and
atomically renamed, then read back and rehashed.

### P1.2 — covariance safety, and capital authority for several strategies

`PORTFOLIO_RISK_ENGINE_V1_2` does two things.

**It closes the covariance gap P1.1 declared.** P1.1 validated symmetry,
finiteness and a non-negative diagonal and said plainly that this does not prove
positive semidefiniteness — a matrix can pass all three and still imply a
portfolio with negative variance, which P1.1 then clamped to zero and reported as
zero volatility. That was the one failure mode in this layer that looked like
perfect safety. P1.2 validates PSD by `LDLᵀ` factorisation with explicit
zero-pivot handling, in pure Python so the package's dependency boundary is
unchanged, at the moment the snapshot is built rather than when some particular
target happens to expose it. A materially negative quadratic form now raises; only
residue inside the existing covariance tolerance normalises to zero. Singular
matrices stay valid, because perfect correlation is a fact rather than a fault, and
nothing is projected onto the semidefinite cone — an indefinite covariance is a
fault in whatever estimated it.

**It lets several strategies request capital through one authority.** Capital
authority is now as deliberate as risk authority:

- **Explicit or refused.** Several strategies need a `StrategyAllocationPolicy`;
  without one the engine still raises, and nothing infers fifty-fifty from there
  being two of them. A strategy the policy does not name is refused rather than
  given a silent zero.
- **Budgets are ceilings.** Unused sleeve capacity becomes cash, never another
  strategy's position — otherwise one strategy's restraint would quietly fund
  another's risk, and the monotonicity the risk engine depends on would break.
- **Exposure aggregates; attribution does not.** Two strategies wanting one
  security hold one position, keyed on `(kind, identity_id)`. When global risk
  reduces the aggregate, attribution reconciles proportionally, and an invariant
  asserts every identity and the portfolio total both reconcile exactly.
- **Global risk is still final.** Sleeve budgets override no cap, count, reserve,
  liquidity limit, volatility ceiling or drawdown band, and all of those apply to
  aggregate exposure — so strategies cannot each consume the whole ADV budget.

A deterministic pairwise positive-correlation concentration control was added
alongside the volatility ceiling: the ceiling addresses total covariance risk, the
pair limit addresses its shape, and neither substitutes for the other. It is not a
factor model, a cluster optimiser or hierarchical risk parity.

Nothing in the package can read a return, a Sharpe ratio or a win rate — asserted
by a test over every identifier. No budget, threshold or correlation limit was
derived from historical performance, and every fixture value exists to force a code
path.

### P1.1 — what the risk authority can now constrain

`PORTFOLIO_RISK_ENGINE_V1_1` adds a portfolio-volatility ceiling from a declared
covariance matrix, position and trade capacity limits from average dollar volume,
and a drawdown overlay that can tighten the gross limit or escalate the operating
mode. **V1 is preserved as a version, not as a former state of the code**: with no
risk snapshot the engine reports V1 and reproduces a V1 decision and its golden
output byte for byte.

Three rules govern the layer:

- **Risk controls only reduce risk.** Every ceiling is seeded at the pre-risk
  weight and only ever lowered, so no later stage can restore exposure an earlier
  one removed. The case that forced it: filling an investable budget would
  otherwise hand a liquidity-capped name's freed budget to another name, leaving
  aggregate risk unchanged and defeating the cap. The freed budget becomes cash,
  and an invariant asserts no approved target exceeds its pre-risk value.
- **Ignorance is not permission.** An unknown volatility is not zero and an
  unknown ADV is not infinite. A missing metric holds the position where it stands
  and still permits a reduction, unless the policy explicitly says otherwise.
- **The overlay can only tighten, and halting does not liquidate.** A multiplier
  above one is refused, a band that weakens at greater depth is refused, and the
  effective mode is the stricter of the base mode and the band's.

The constraint order is frozen and documented in
[the reference page](portfolio-risk-engine.md); reordering it changes results, so
it is an engine-version change rather than a refactor.

P1.1 forecasts nothing. It consumes declared volatility and covariance with their
fingerprints, estimates no market impact, and chose no threshold from history —
`EXAMPLE_ADVANCED_RISK_POLICY_V1` is `NOT_PRODUCTION_AUTHORIZED` and every number
in it is a test constant.

`PORTFOLIO_RISK_ENGINE_V1` is a foundation, not a production risk system.
`EXAMPLE_POLICY_V1` is `NOT_PRODUCTION_AUTHORIZED`: its limits exist to make the
engine's behaviour observable, and none of them has been established as correct for
live capital. Choosing live limits is a dedicated risk-policy phase with its own
evidence, and no threshold here was selected by running historical performance.

**Previous checkpoint — R1.12, the prospective capture pipeline**, on
`feat/r1-backtest-harness`, branched from `feat/phase-b2-storage-abstraction` at
`06357c5` (tagged `pit-research-dataset-v2`).

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.11 | **COMPLETE** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED** (R1.3) |
| H2 | **unchanged** — rule, band, cadence, P1–P4, aggregate rule and window length all as frozen |
| September 30 | **MISSED / NOT BACKFILLED / NOT BACKFILLABLE** — a hard refusal, now enforced before any value is computed |
| V1 protocol | **IMMUTABLE** (`60c17136…`), superseded prospectively by V2 |
| V2 protocol | **AUTHORITATIVE FOR THE FUTURE WINDOW** — semantically unchanged by R1.12 |
| Capture executors | **IMPLEMENTED** — stages D and E have production callers; R1.11 found zero |
| Readiness | two definitions: `SOURCE_AND_DERIVABILITY` and `EXECUTABLE_CAPTURE_READINESS` |
| Temporal source rule | **`CURRENT_AT_RETRIEVAL` sources must be captured at or before the decision instant** |
| Calendar binding | bound to the 1,531-session research source; a thinner source raises `CALENDAR_BINDING_MISMATCH` |
| Valid prospective observations | **0** |
| Missed eligible opportunities | **1** |
| Next eligible decision | **2026-12-31**, instant 16:00 ET, derived from the calendar |
| R1.10 activation and arm | **SUPERSEDED_BY_SEMANTIC_REPAIR** — unedited, but no longer authoritative |
| Current activation and arm | bound to the R1.12 repair commit; the records and their digests live under `.evidence/prospective/h2_v1/` and `.evidence/research/r1_12/`, as every prior activation has |
| State | **CAPTURE_READY / ARMED_WAITING_FOR_DECISION** |
| Next | operational readiness checks only; capture the first valid decision once, at the authorised instant |

Three generations of readiness, and what each actually claimed:

| Phase | Readiness claim | What was true |
| --- | --- | --- |
| R1.8 | — | no prospective source path existed at all; the 2026-09-30 decision was missed |
| R1.9 / R1.10 | `SOURCE_AND_DERIVABILITY` — reported as 6/6 READY | the three fetched sources were live; the three derived inputs were asserted derivable with null fingerprints and no producer |
| R1.11 | the same, re-certified | `CAPTURE_STATE_MACHINE_INCOMPLETE` — stages D and E had no executor, and the declared derivation was impossible |
| R1.12 | `EXECUTABLE_CAPTURE_READINESS` | every required artifact is produced by a real producer and carries a non-null fingerprint; both executors have production callers |

Earlier reports are not rewritten. Each was accurate under the definition in force
when it was made, and naming the definitions apart is what stops the weaker claim
being mistaken for the stronger one again.

What R1.11 found, and what R1.12 repaired. R1.11 certified the armed window with
every gate green — bindings matched, the calendar replayed 1,531 frozen sessions,
the live source canary returned 6/6 READY, preflight said `GO` — and then found
that no decision could be produced. `decision_packet()`, `execution_packet()` and
`paired_targets()` had **zero production callers**, and stages `D_DECISION` and
`E_EXECUTION_REFERENCE` were specified and implemented nowhere.

Underneath that sat a deeper fault: the declared derivation was impossible. The
old readiness marked `score_inputs`, `tradability` and `ranked_cross_section`
READY whenever the three fetched sources were admissible, with
`fingerprint: null` and the note *"derivable once its sources are admissible"* —
but no fetched source carried a reported fundamental value. The universe is a
broker asset list, the market snapshot is daily bars, and `sec_availability`
holds only acceptance metadata. "6/6 READY" asserted a derivation nothing could
satisfy.

R1.12 supplies the missing fetch and the missing executors. Reported values come
from `get_filing_observations`, joined to the acceptance snapshot by accession so
a value participates only when the filing carrying it was public at the instant;
the frozen `NON_PIT_SAFE_OPERATIONS` refuses the *merged* `get_financial_statements`
and not this one, because the merged view destroys the availability date. Corporate
actions recover the share-count price basis. Both are fingerprinted inside
`score_inputs`' provenance, so the required count stays at six.

Readiness is now two questions, named apart: `SOURCE_AND_DERIVABILITY` asks
whether the fetched sources are admissible, and `EXECUTABLE_CAPTURE_READINESS`
asks whether a producer has emitted a validated, fingerprinted artifact. A merely
derivable component is `BLOCKED`, and nothing is ready with a null fingerprint.
R1.9's and R1.10's reports are not rewritten; they were accurate under the
definition they were made with.

**The repair moved the semantic fingerprint**, from
`0c9a594b5392eaf1279bcda0848bba48c681392745764ebe800253a7d24e4e9e` (19 modules)
to a 30-module fingerprint covering the capture path. The R1.10 activation and
arm are therefore `SUPERSEDED_BY_SEMANTIC_REPAIR` — left unedited, and correctly
reporting `activation_mismatch` / `arm_mismatch` until a new activation and arm
are taken on the R1.12 commit.

One strategy implementation. Selection runs through
`membership_hypothesis.select`, ranking through `membership_hypothesis.rank`, and
scoring through `domain.score_company` on metrics from
`domain.build_company_metrics` and periods from
`api_clients.periods_from_observations` — the same functions the historical
reconstruction calls. Nothing is reimplemented prospectively, because a copy
could drift from the frozen original without a test failing.

What R1.10 closed. R1.9 built the sources and left a timing gap wide enough for
the September failure to recur. Its admissibility check asked only whether a
snapshot's facts predated the instant — and a **current-only snapshot satisfies
that trivially, because its semantic time is its retrieval time**. A universe
fetched at 23:00 would have passed for a 16:00 decision.

Sources are now classified by what they can say about a moment other than now.
`SOURCE_HAS_EVENT_TIME` (SEC acceptance) and `SESSION_BOUNDED` (a completed daily
bar) may be fetched whenever, because each fact carries when it was true.
`CURRENT_AT_RETRIEVAL_ONLY` (the broker asset list, tradability) may not: its
latest admissible retrieval is **the decision instant itself**, and a later one
is refused as `CURRENT_ONLY_SOURCE_TOO_LATE`. The single escape is a
provider-supplied effective time at or before the instant — it must come from the
provider, since one we assign ourselves is the retrojection the rule forbids
wearing a field name. An envelope declaring no class defaults to the most
restrictive, so forgetting fails closed.

**The capture sequence is frozen before the arm**, so the decision day has
nothing left to decide: Stage A takes the universe and tradability *before* the
instant and seals them; Stage C completes the session bar and the
acceptance-bounded SEC retrieval afterwards; the score is computed last, because
what matters is that every input was admissible at the instant and the arithmetic
adds no information. **Stage A is the one stage that cannot be recovered** —
prices and filings can be fetched late, the universe cannot.

**A defect R1.9 shipped, caught here by verifying V2 against the artifact rather
than the report.** R1.9's generator imported H2's *historical* aggregate rule
(`C1 AND C2 AND C3 AND C4`) in place of the *prospective* one
(`P1 AND P2 AND P3 AND P4`). Nothing had been armed under it, so it was a draft
that failed verification rather than an authority being rewritten; the corrected
V2 is emitted by R1.10 and both digests are recorded. A regression test now
asserts the criteria and the aggregate expression name the same things.

**The missed opportunity is counted apart from the observations**, so neither can
absorb the other: valid observations 0, missed eligible opportunities 1, and the
next successful capture is `VALID_OBSERVATION_1` rather than a renumbering of
history. The window requirement is unchanged at five decisions and four
transitions — a source-plane failure is not a reason to ask less of a hypothesis.

The preceding checkpoint, **R1.9**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.7 | **COMPLETE** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED** (R1.3) |
| H2 | **unchanged** — `PENDING PROSPECTIVE EVIDENCE`; not failed, not supported |
| R1.8 | **FIRST ELIGIBLE PROSPECTIVE CAPTURE MISSED** |
| Reason | **`PROSPECTIVE_DATA_PLANE_NOT_READY`** — an operational readiness defect, not an H2 result |
| September 30 recovery | **PROHIBITED / NOT RECOVERABLE / NOT BACKFILLED** |
| Prospective evidence | **0 valid observations** |
| R1.9 | prospective source plane **READY** — 6 of 6 inputs, proven against live providers |
| Protocol disposition | **`P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED`** |
| V1 | **IMMUTABLE AND UNEDITED** |
| Successor | **`H2_PROSPECTIVE_SHADOW_PROTOCOL_V2`** — governance only; H2 untouched |
| Re-arm | **REQUIRED** — two independent reasons |
| Prospective verdict | **PENDING**; criteria `SEALED_UNTIL_WINDOW_COMPLETE` |
| Next eligible capture | derived from the benchmark calendar, not assigned here |
| Next | **R1.10 — certify the next eligible capture window and arm under V2** |

What R1.8 cost, and why. The 2026-09-30 decision was eligible and the runner was
armed five days early, but no honest packet could be produced: five of the six
required decision-packet fingerprints had no source path, and the newest prices
in either database were 33 days older than the decision session. Failing closed
was correct. **One quarter of a five-decision window is gone and cannot be
recovered**, because V1 permits reconstruction only of a quarter whose
decision-time state was already durably captured, and it was not.

What R1.9 built. Four live adapters, an immutable content-addressed store, and a
readiness gate that is conjunctive — five of six reads BLOCKED, not nearly ready,
because a decision packet needs all six fingerprints. The invariant at the centre
is one rule in one direction: **late retrieval is permitted, late information is
not.** Every snapshot carries `retrieved_at` and `semantic_as_of` separately and
a naive timestamp is refused, since it cannot be compared to an instant.

SEC availability is the one genuinely point-in-time source — the submissions
index publishes an acceptance timestamp per accession, so `accepted_at <= T` is
provable rather than asserted, and `companyfacts` is refused by name because a
current aggregate cannot attribute a value to an accession. Prices are bounded by
session, with later bars dropped and counted. The universe carries no history at
all, so its as-of is its retrieval moment and is never backdated.

**A constraint surfaced while proving the plane**, and it changes how a capture
must be scheduled: a current-state source can only serve an instant at or after
its retrieval, so the universe must be snapshotted **at or before** the decision
instant. The capture job must run *at* 16:00, not later that evening. Prices and
SEC acceptance may be fetched afterwards; the universe may not.

What the governance audit found. V1 settles that September 30 can never count.
It does **not** settle whether the window continues: its "skip that quarter"
clause is triggered by a missed *arm cutoff*, and this cutoff was satisfied, so
borrowing that disposition would be an interpretation rather than a reading — and
the convenient one. Hence `P3`, and a successor protocol that states the
disposition explicitly while leaving V1 untouched.

**V2 changes nothing about H2** — not the rule, the band, the cadence, P1–P4, the
aggregate rule, or the window length. It adds a named disposition for a
capture that fails closed on an unavailable source, distinguishing a
pre-sequence miss (no transition lost; the window starts later) from a
mid-sequence one (the chain the criteria measure is broken), and makes readiness
a **precondition of arming** rather than a hope at capture.

**Re-arm is required for two independent reasons**: V1's arm record names
2026-09-30, a date the runner can no longer act on; and building the source plane
moved the semantic code fingerprint from `fc4b10f1…` to `164b445e…`, which on its
own closes the V1 window, since the runner refuses confirmatory capture across a
semantic change.

The preceding checkpoint, **R1.7**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.6 | **COMPLETE** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED** (R1.3) |
| H2 historical exploratory test | **SUPPORTED** (R1.5), `EXPLORATORY_SUPPORT_ONLY` |
| H2 candidate | **`H2_MEMBERSHIP_STABILITY_CANDIDATE_V1`** — **FROZEN**, unchanged by R1.7 |
| Prospective protocol | **`H2_PROSPECTIVE_SHADOW_PROTOCOL_V1`** — **FROZEN**, unchanged by R1.7 |
| Prospective runner | **`PROSPECTIVE_SHADOW_RUNNER_V1` — ACTIVE_AND_ARMED** |
| Activation | **RECORDED** — one immutable record bound to this commit; see `prospective_activation.json` and the control mirror |
| First eligible prospective decision | **ASSIGNED_BY_PROTOCOL** — derived at arming from the activation instant and the session calendar; recorded in `prospective_arm_record.json`. Never manually supplied |
| Prospective observations | **0** |
| Completed transitions | **0** |
| Prospective criteria | **SEALED_UNTIL_WINDOW_COMPLETE** — P1–P4 each `NOT_YET_EVALUATED` |
| Prospective verdict | **PENDING** |
| Paper trading | **NOT AUTHORIZED** |
| Live trading | **NOT AUTHORIZED** |
| Next | **R1.8 — capture the first eligible prospective H2 decision when due** |

What R1.7 built. A runner that waits, and that is hard to misuse a year from
now when nobody remembers why a rule was there.

**It cannot trade.** `order_capability_reachable` parses each module and
inspects imported names, attribute accesses and call targets rather than
grepping text — capability is a property of code, so a string mentioning a
broker is data while an import is an ability. `bind_runner` calls it before
changing any state, no `force` parameter exists anywhere in the package, and no
broker credential is read or required.

**The calendar is checked, not asserted.** The frozen dataset's benchmark
sessions stop where the dataset stops, so `NYSE_SESSION_CALENDAR_V1` writes the
rules out — weekday, minus rule-derived holidays including Good Friday from the
Easter computation and Juneteenth from 2022, minus known irregular closures,
with a Saturday New Year's Day not observed. Replayed over the frozen record it
reproduces all **1,531** benchmark sessions from 2020-07-27 to 2026-08-28 with
zero additions and zero drops, and `bind_runner` refuses if it ever disagrees.

**Activation cannot be backdated or repeated.** The instant comes from the
runtime clock; a supplied timestamp is an explicit error rather than a validated
convenience. It binds to a full immutable commit, because prospective packets
bind to code and uncommitted code cannot be bound to — the CLI refuses a dirty
tree for the same reason. A restart re-reads the stored record: idempotency keys
on what is stored rather than on a freshly stamped one, since the clock moves
between calls and a restart must not look like a second activation. What must
match is the binding — commit, candidate, protocol, contract, semantic
fingerprint. **Activation is not an observation.**

**The first decision is derived, never chosen.** It is the first quarter-end
session strictly after activation for which the runner was armed no later than
the close of the immediately preceding session. Both conditions are required;
arming exactly at that close satisfies the cutoff and one minute after does not.
A quarter failing it is recorded in `skipped_quarters` and never enters the
sequence, so there is nothing to reconstruct later. The arm record exists
because a later decision claiming the runner was armed is worth nothing unless a
durable record predates it, and it is mirrored immediately for that reason.

**The state machine skips nothing.** `ARMED` leads only to
`WAITING_FOR_DECISION`, so no path reaches a captured decision without passing
through waiting for the decision instant. The observation count is derived from
the packets that exist rather than incremented, so a duplicated call cannot
advance it, and a restart recovers activation, arming, the first decision, the
counts and the verdict from durable state alone.

**A semantic change halts capture.** Thirteen modules that can affect the
universe, features, score inputs, ranking, selection, target count, identity,
execution reference or accounting contribute to one fingerprint; logging and
presentation do not. A moved fingerprint classifies as
`SEMANTIC_RESEARCH_CHANGE` and refuses confirmatory capture until reviewed,
because remembering which changes matter is not a control.

**Decision and execution packets are separate records**, and that separation is
load-bearing: merging them would put a price the decision could not have seen
inside the record of that decision. A decision packet rejects every outcome
field at construction; an execution packet links by digest and declares
`modifies_decision_packet: false`; a correction links to both and changes
neither, and a material one yields
`PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT` rather than a fix.

**Nothing was observed.** Zero decision packets exist and none may exist until
the derived decision instant actually arrives. Ending armed with zero
observations and a sealed verdict is the successful outcome of this phase.

The preceding checkpoint, **R1.6**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.5 | **COMPLETE** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED** (R1.3). Not reopened |
| H2 historical exploratory test | **SUPPORTED** (R1.5) — C1–C4 all met |
| H2 candidate | **`H2_MEMBERSHIP_STABILITY_CANDIDATE_V1`** |
| Candidate status | **FROZEN** — `FROZEN_CANDIDATE`, `PROSPECTIVE_CONFIRMATION_PENDING`, `NOT_PRODUCTION`, `NO_LIVE_CAPITAL` |
| Rule | **`ONE_DECILE_HYSTERESIS_V1`**, unchanged |
| Historical evidence | **`EXPLORATORY_SUPPORT_ONLY`** — and not upgradable by further work on the frozen dataset |
| Historical development | **CLOSED** — further widths, frequencies, weightings and holding rules all PROHIBITED |
| Independent confirmation | **PENDING** |
| Prospective protocol | **`H2_PROSPECTIVE_SHADOW_PROTOCOL_V1`** — FROZEN, runner NOT_YET_ACTIVE |
| Prospective observations | **0** (0 completed transitions, verdict PENDING) |
| Broker / paper / live | **NOT AUTHORISED** at any level |
| Next | **R1.7 — activate the prospective H2 shadow runner** |

What R1.6 froze. The candidate is frozen **because the preregistered mechanism
passed**, and the record says so alongside the thing it is not frozen for: H2's
1.35 percentage point edge over the comparator, across seventeen rebalances on
one realised path with a marginally lower Sharpe, is noise, took no part in the
verdict, and justifies nothing.

Everything the frozen dataset can still offer is now closed. The historical
parameter search is `CLOSED`; additional retention widths, frequencies,
weightings and holding-period rules are `PROHIBITED` under this candidate id;
`historical_clean_holdout` is `NONE`. A new variant would need a new hypothesis
identifier and would still carry `EXPLORATORY_SUPPORT_ONLY`, because R1.3
derived the hypothesis from the whole period.

The protocol that replaces it is fixed before any observation exists to shape
it. **Five decisions, four transitions**, and the window cannot move in either
direction — no stopping early on success, no extending on a poor result, no
choosing the best four afterwards, no dropping or retrospectively replacing a
transition. **P1 and P2 are paired and directional**: they compare H2 against
the comparator on the same prospective cross-sections, never against the
historical 1.6177×, because requiring that magnitude would fit the prospective
test to an effect size the generating data produced. P3 protects exposure, P4
makes protocol integrity itself a pass condition — a rewritten decision packet
fails the test rather than being absorbed by it. `PROSPECTIVE_MECHANISM_CONFIRMED`
requires **P1 AND P2 AND P3 AND P4**, and `interpret_prospective` returns
`PENDING` until every transition is in.

**The first eligible decision is a rule, not a date.** Nothing computes a
calendar: it is the first quarter-end whose decision instant falls strictly
after activation and for which the runner was armed by the preceding session's
close. A quarter that misses that is skipped, and any reconstruction carries
`RETROSPECTIVE` and `NOT_PROSPECTIVE_CONFIRMATION` and never enters the
criteria.

`PROSPECTIVE_SHADOW_CONTRACT_V1` writes out the bridge to a live stream rather
than assuming the historical contract governs it. The asymmetry it exists for is
the direction of error: historically a late filing was simply absent;
prospectively it may arrive minutes after the decision instant and be sitting
there when anyone looks. Observations live in `PROSPECTIVE_SHADOW_STREAM_V1` and
must never be written into `PIT_RESEARCH_DATASET_V2`.

**Nothing was observed and nothing was authorised.** Zero decision packets
exist, and the package cannot produce one — no simulator, data client or broker
is importable from it, proved by reading its own source at bind time. Shadow
means internally simulated reference execution; broker paper and live capital
remain unauthorised at every level of the ladder.

The preceding checkpoint, **R1.5**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.3 | **COMPLETE** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED** (R1.3). Not reopened |
| R1.3 cause | membership drift — `M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT` |
| R1.4 | **COMPLETE** — H2 preregistered, nothing run |
| R1.5 | **EXECUTED** — one variant, one run, spec digest `6d266448…` unchanged |
| H2 | **`H2_MEMBERSHIP_STABILITY`** |
| Rule | **`ONE_DECILE_HYSTERESIS_V1`** — entry at the ordinary top-decile count, retention inside two selection widths |
| Mechanism verdict | **SUPPORTED** — C1, C2, C3 and C4 all met under the frozen rule `C1 AND C2 AND C3 AND C4` |
| Historical classification | **`EXPLORATORY_HISTORICAL_TEST`** |
| Independent confirmation | **STILL REQUIRED** — `NOT_PROVIDED_BY_R1_5`, and not obtainable from this dataset |
| Next | **R1.6 — freeze the H2 candidate and define a prospective shadow-test protocol** |

What R1.5 measured. Over the same 17 quarter ends, the same executable
cross-sections and the same target counts as the comparator, annual one-way
turnover fell from **2.253982668222645× to 1.6177403954997098×** — a **28.2%**
reduction — and the entry/exit component fell from 2.114115× to
**1.411599×**. The decomposition reconciles exactly: entry 0.796895, exit
0.614704, resizing 0.206142, summing to the total with **0.0** unexplained.

**The reduction came from where the hypothesis said it would.** Attributing the
0.636242× difference: entry −0.344063, exit −0.363271, and resizing
**+0.071092** — resizing *rose*, because holding names longer leaves more weight
drift to correct at each rebalance. No component was forced to be positive.

Quarter-to-quarter retention rose from **45.77% to 64.40%** (+18.63 pp, +40.7%
relative), mean holding duration from 1.73 to **2.48 quarters**, and re-entry
episodes fell from 35.4% to 23.6%. Mean Jaccard overlap with the ordinary
top-decile set is **0.707**, so roughly 29% of the portfolio differs at a typical
rebalance; **240 displacement pairs** were recorded, mean rank difference 48.1
positions and mean score difference 3.22 points.

**Performance decided nothing and barely moved.** Gross total return +127.9%
against the comparator's +126.6% (+1.35 pp), CAGR 22.52% vs 22.34%, Sharpe 0.979
vs 0.981, max drawdown −23.64% vs −24.16%. SPY over the same sessions returned
+67.6%. The per-side cost break-even rises from 156.9 to **222.6 bps**. None of
this entered the verdict: `evaluate` takes turnover, exposure and safety and has
no parameter a return could arrive in.

**What this is not.** R1.3 generated H2 from the whole frozen period, so no part
of this result is out-of-sample. Every artifact carries
`EXPLORATORY_HISTORICAL_TEST` and `independent_confirmation:
NOT_PROVIDED_BY_R1_5`. The legacy blocks are reported as `LEGACY_DEV_BLOCK`
(+9.2 pp relative), `LEGACY_VAL_BLOCK` (**−7.4 pp**) and `LEGACY_HOLDOUT_BLOCK`
(+46.0 pp, `CONTAMINATED_FOR_H2_CONFIRMATION`), each marked
`DESCRIPTIVE_ONLY_FOR_H2`. The validation block still trails its benchmark, as
it did under the comparator, and nothing in H2 addresses that.

The preceding checkpoint, **R1.4**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 – R1.2 | **COMPLETE** |
| R1.3 | **COMPLETE** — forensic analysis; H1 `H1_MECHANISTICALLY_FALSIFIED`, no defect |
| R1.4 | **COMPLETE** — preregistration only; **0 strategies simulated, 0 return or turnover calculations** |
| H1 | **CLOSED / MECHANISTICALLY FALSIFIED.** Not reopened |
| R1.3 cause | membership drift — `M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT` |
| H2 | **`H2_MEMBERSHIP_STABILITY`** — `PREREGISTERED_NOT_EXECUTED` |
| Rule | **`ONE_DECILE_HYSTERESIS_V1`** — one parameterisation, no alternative evaluated |
| Aggregate rule | **C1 AND C2 AND C3 AND C4**, fixed before execution — unlike H1, which stated none |
| Historical evidence status | **EXPLORATORY ONLY.** R1.3 read the whole frozen period, so no part of it can confirm H2 |
| Independent confirmation | **REQUIRED** after the exploratory test, and not obtainable from this dataset |
| Next | **R1.5 — execute the single preregistered H2 exploratory test** |

What R1.4 froze. H2 claims that holding the quarterly cadence, the score, the
target size, the weighting, the execution timing and every data semantic fixed,
a one-decile membership-hysteresis rule reduces turnover by reducing rank-driven
entries and exits while preserving exposure. **It makes no performance claim**,
and no performance measurement takes part in its verdict.

The rule adds exactly one idea to R1.2: a name already held survives a smaller
rank decline than would be required to buy it. Entry is the ordinary top-decile
target count; an incumbent is retained while its rank position is inside
**twice** that count. Both numbers are structural rather than selected — the
target count is taken from R1.2 and never recomputed, so both strategies always
hold the same number of names, and the band is two selection widths expressed in
names, which equals the top 20% by construction while inheriting the entry
rule's rounding. R1.3 measured where entrants and exits sit relative to the
cutoff and deliberately chose no width from those distributions.

Four criteria are frozen: turnover strictly below 2.253982668222645×, entry/exit
churn turnover strictly below 2.11411510465003× (an absolute quantity, since a
share can fall while the quantity rises), the same target-count rule with zero
intentional cash quarters, and zero accounting, identity, leakage or same-close
violations. H1's 1.95× is **not** an H2 criterion — R1.3 showed it to be an
empirical anchor times an undeclared one half — and is carried only as
`LEGACY_H1_TURNOVER_REFERENCE`. The binder refuses any specification that
promotes it to a criterion or moves it.

**No part of the frozen history can confirm H2.** R1.3's analysis spanned
development, validation and the already-read holdout, so the dataset is
hypothesis-generation data for H2. A later historical run must be classified
`EXPLORATORY_HISTORICAL_TEST`; `clean_h2_holdout_remains` is false, no subperiod
is relabelled, and `CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST` is true. No
prospective duration is chosen, because no research policy defines one.

**No H2 performance appears anywhere, because none exists.** The package imports
no simulator and no pricing path, and `simulation_reachable` proves it by
reading its own source before anything else runs.

The preceding checkpoint, **R1.3**, stands unchanged.

| | |
| --- | --- |
| B2 | **COMPLETE / FROZEN** |
| R1.0 | **COMPLETE** — harness and four preregistered baselines |
| R1.1 | **COMPLETE** — interpretation; one experiment specified, not run |
| R1.2 | **COMPLETE** — that experiment executed unchanged; H1 `PARTIALLY_SUPPORTED_BY_R1_2` |
| R1.3 | **COMPLETE** — forensic analysis only; **0 strategies simulated, 0 parameters tested** |
| Failed premise | **C3 — annual turnover ≤ 1.95×**; observed **2.253982668222645×**, reproduced bit-exactly from the ledgers |
| Mechanism | `M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT` |
| `H1_MEDIUM_HORIZON_SELECTION` | **`H1_MECHANISTICALLY_FALSIFIED`** — the criterion was unreachable by frequency alone. Not a defect, and not a verdict on the score |
| Implementation / accounting defect | **NONE** — one cosmetic metadata miscount found and recorded; it touches no criterion |
| Independent confirmation | **NOT YET** — unchanged; the R1.2 holdout remains `CONTAMINATED_FOR_H1_CONFIRMATION` and was not upgraded |
| Current-classification limitation | **MATERIAL / UNRESOLVED** — 99.0% of executable observations, 98.4% of selections, 98.3% of portfolio weight |
| Next | **R1.4 — define the next preregistered hypothesis** from the turnover-mechanism evidence. No rule is chosen yet |

What R1.3 found. **A quarter of the rebalances is not a quarter of the
trading.** Per-rebalance one-way turnover rose by a factor of 1.796 (0.3137 →
0.5635) because the ranking drifts further over three months than over one, and
that gave back **39.8%** of the reduction frequency alone would have delivered:
had per-event turnover held at the monthly level, quarterly would have annualised
at 1.255× and passed comfortably.

**The criterion was unreachable, which is why H1 is falsified rather than
merely unmet.** An exit must be sold in full and an entrant bought to its
target, so both are forced by the target set; only resizing a position held on
both sides is avoidable. Removing *every* resizing trade while keeping the
executed quarterly selections unchanged still leaves annual turnover at
**2.1242×**, above the 1.95× ceiling by 0.174×. No implementation could have
satisfied C3 without changing which identities are selected — which is not what
H1 proposed.

**The number was already in the hypothesis record.** Quarter-to-quarter
retention is 45.77%, and the monthly ledger's own three-month survival is
45.98% — the same quantity over the same elapsed time, agreeing to 0.2 pp. H1's
premise B recorded that figure as `three_month_retention: 0.4598` before the
experiment ran. Quarterly rebalancing did not cause the drift; it only stopped
trading through it.

**Churn is not boundary noise.** Only 14.4% of membership changes involve a rank
move under 5 percentile points and 34.2% under 10; 37.6% move more than 20
points, and the median move is 14.9 points. Half of entrants (49.6%) arrive from
beyond the 20th percentile and 57.5% of exits fall beyond it. 87.0% of exits and
75.6% of entries are ranking decisions; the rest are availability changes —
tradability 6.2% of exits and 16.2% of entries — so the score is not responsible
for all of it. Resizing is 5.98% of traded notional and terminal legs 0.23%.

Two reporting notes, recorded rather than fixed. R1.2's comparator field
`rebalances_in_window` reads 50 where 49 is correct: it counts distinct month
labels among in-window trades, and a terminal settlement carries an empty month
label. It feeds no criterion and no turnover figure. Separately, the frozen
record annualises turnover two ways — nominal frequency for the quarterly figure
and C3's 3.90× anchor, elapsed years for the matched comparator — which is why
that comparator reports 3.7949× where the nominal convention gives 3.7642× for
the same window. C3's verdict is invariant to the choice, and the convention
actually used *understates* quarterly turnover, since 17 rebalances over 4.056
years is 4.19 a year rather than the nominal 4.00.

The preceding checkpoint, **R1.2**, stands unchanged.

What R1.2 found. Over 17 quarter ends (2022-03-31 … 2026-03-31) and 1,023
sessions, the quarterly portfolio returned **+126.5% gross** (CAGR 22.3%,
Sharpe 0.98, max drawdown −24.2%) against SPY's +67.6% on the same sessions,
and against R1.0's own monthly top-decile ledger sliced to those sessions
(+103.5%, CAGR 19.1%, Sharpe 0.88, max drawdown −25.9%). Quarterly beat matched
monthly by 23.1 pp of total return with a shallower drawdown, and its per-side
cost break-even roughly doubled from 77.9 bps to **156.9 bps**. The medium-horizon
premise held: `D10 − D1` at the frozen 60-session horizon is +2.02 pp with a
rank association of +0.48, rising to +6.43 pp at 120 sessions.

**The premise that failed is the one the hypothesis was named for.** Annual
turnover fell from 3.90× to **2.25×** — a 42.2% reduction, short of the ≥50%
the frozen criterion required — because per-rebalance turnover *rose* from 0.33
to 0.56 as the ranking drifts further over three months than over one. The
threshold was not moved. One other measurement is worth naming: the validation
block is the single block with a negative benchmark-relative return (−7.3 pp),
so criterion 4 passed at exactly its threshold of two blocks out of three.

The checkpoint before it, **R1.1**, stands unchanged.

| | |
| --- | --- |
| B2 evidence / PIT | **FROZEN / COMPLETE** |
| R1.0 | **COMPLETE** — harness and four preregistered baselines |
| R1.1 | **COMPLETE** — interpretation only; no strategy executed |
| Compounder evidence status | **`STRONG_ENOUGH_FOR_HYPOTHESIS_TEST`** — all eight rubric conditions met. Not a statement that the effect is established: both headline t-statistics are below 2 |
| R1.0 holdout | **VALID FOR R1.0 ONLY**; `HOLDOUT_ALREADY_OBSERVED` for any hypothesis derived afterwards. No untouched historical holdout exists in the frozen dataset |
| H1 | `H1_MEDIUM_HORIZON_SELECTION` — **accepted for one experiment**, all five premises `SUPPORTED` |
| Next | **R1.2 — test the preregistered `COMPOUNDER_QUARTERLY_TOP_DECILE_V1`**, specified and *not* executed |

What R1.1 found, in one paragraph. The frozen score's cross-sectional signal
strengthens monotonically with horizon — mean monthly rank IC +0.017 at one
session, +0.025 at twenty, +0.063 at a hundred and twenty — and top-decile
membership is sticky, with a month-to-month rank correlation of 0.909 and 71.5%
of the top decile still there a month later, 46.0% three months later. That
combination is what `H1` rests on. Against it: the monthly `D10 − D1` averages
+0.84 pp with a t-statistic of 1.66 and a bootstrap interval of
[−0.15, +1.79] pp; 2026's three months are negative on both headline measures;
and in the R1.0 holdout `D10 − D1` collapses to +0.01 pp with only a third of
months positive. That collapse is explained rather than explained away: the
holdout's decile profile is **U-shaped**, both tails beating the middle, so the
long-short contrast cancels while `D10 − cross-section mean` — the quantity a
long-only portfolio actually earns — holds at +0.55 pp/month. Both measures and
the full ten-decile profile are published for every block. Turnover is 88.7%
ranking churn, which is what makes a lower-frequency test worth running, and
the analytic per-side cost break-even against SPY is 77.9 bps.

The preceding checkpoint, **R1.0 — the leakage-safe backtest harness built and
the first frozen baselines executed**, stands unchanged.

| | |
| --- | --- |
| B2 evidence / PIT | **FROZEN** — `SEC_EVIDENCE_GENERATION_V2`, acquisition closed at B2.17 |
| R1 | **ACTIVE** |
| Backtest harness | **COMPLETE** — `stock_screener.backtest_harness`, bound to `BACKTEST_DATA_CONTRACT_V1`, fail-closed, no `--force` |
| Frozen research split | `RESEARCH_SPLIT_V1`, SHA-256 `b58283be55428b1a…` — DEV 2022-01…2023-12 (24), VAL 2024 (12), HOLDOUT 2025-01…2026-03 (15), no overlap |
| Preregistration | `r1_0_preregistration.json`, SHA-256 `8a19f0fdc91f92fa…`, written and hashed before any performance |
| Baselines executed | `BASELINE_PASSIVE_V1`, `BASELINE_UNIVERSE_EQUAL_WEIGHT_V1`, `COMPOUNDER_DECILES_V1` (diagnostic), `COMPOUNDER_TOP_DECILE_V1`, each gross and under `SIMPLE_10BPS_PER_SIDE_V1` |
| Holdout | evaluated once, after `first_holdout_access` proved the preregistration on disk hashed as presented |
| Next | **R1.1 — interpret baseline signal quality and define the first hypothesis-driven strategy experiment** |

The run: 51 monthly decision dates (2022-01-31 … 2026-03-31), a valuation
window of 1,065 sessions (2022-02-01 … 2026-04-30), 164,193 universe
observations of which 39,001 reached `EXECUTABLE`, 51 rebalances per
portfolio, zero accounting violations, zero empty cross-sections, 33 terminal
legs resolved by identity (30 settled, 3 unknown), six schema-valid run
manifests, and 37 semantic fingerprints identical across two independent
executions. **Research results, not product conclusions:** over the whole
window and gross, SPY returned +68.2% (CAGR 13.10%, Sharpe 0.79, max drawdown
−22.1%); the equal-weighted point-in-time universe +43.5% (CAGR 8.94%, Sharpe
0.52, −24.0%); the frozen top-decile `COMPOUNDER_V1_2` portfolio +117.7%
(CAGR 20.24%, Sharpe 0.92, −25.8%, annual turnover 3.90×). At the
preregistered 20-session horizon the decile means rise from +0.56% (D1) to
+1.31% (D10); `D10 − D1` averaged over 51 monthly cross-sections is +0.84
percentage points with a standard error of 0.51 pp (t = 1.66, bootstrap CI
[−0.15, +1.79] pp), and the mean monthly rank IC is +0.025 (t = 1.76, CI
[−0.003, +0.053]), weakest in the holdout at +0.011. The direction is
consistent and the evidence is weak; R1.1 interprets it.

The preceding checkpoint, **B2.18 — the V2 PIT dataset frozen and the
backtest-readiness contract established; the SEC/PIT evidence program is
CLOSED**, stands unchanged on `feat/phase-b2-storage-abstraction`. **The BEK
drive is permanently unavailable** and Generation V1 is history that
creates no trust. B2.9X started **Generation V2** from what survived on the
internal disk; B2.10 proved the pipeline on chunk 000; B2.11 built chunks
001–035 and closed the baseline; B2.12 read the whole corpus back out of
the bucket into the derived index; B2.13 reconstructed the lost assembly
harness and froze Plan A3's inputs; B2.14 ran the unchanged planner over
them. **V1 unavailable; V2 baseline COMPLETE; 36/36 chunks;
38,146 trusted artifacts — 5,733 submissions catalogues and 32,413 filing
documents — 37,141,434,863 bytes; manifest SHA-256
`13722e021e2d4e03d13784c5c829320bad5b7dc74055378057bf8804d356086b`;
`BASELINE_COMPLETE` (`6c25b3c3…`) written and mirrored. Derived index
COMPLETE: `<SEC_CONTROL_DIR>/derived/sec_evidence_index_v2.db`,
234,164,224 bytes, SHA-256
`3deb46b746c5be7be0bef05949188dfaa6dca733f24fbb37e589cda0c670d68d`;
38,146 / 38,146 inputs ledgered `COMMITTED`, every body verified against
the manifest before it was parsed, 0 parse failures, `integrity_check` ok;
32,413 filing rows, 43,001 security rows, 716,290 catalogue rows, 5,733
catalogue-stats rows; semantic fingerprints `catalogue 98f66a22…`,
`filings 8fb487b3…`, `securities 98083e2c…`, `cover a0c00e69…`,
`catalogue_stats a809c94c…`; metadata, fingerprints and the completion
marker mirrored as snapshots and read back. A3 PlanningInputs V2 FROZEN:
`plans/generation_v2/a3_inputs/`, package SHA-256
`83680eadaa06317c10a0a26e68f5547a022207dc1bc1e5c1283a8427b5db3ce5`, bound
to `stage9k.db` `20a77d24…`, universe `b8eaed3e…`, resolution `161429f5…`,
manifest `13722e02…`, index `3deb46b7…` and its five fingerprints; 268,780
universe months → 16,194 `COVERED`, 242,269 open (166,075 `G1_SOLVABLE`,
raw `UNKNOWN` floor 76,194); 279,772 candidates carrying 657,826 cover
keys over exactly the solvable gaps; 32,413 held; deterministic across two
in-process assemblies and a second invocation; 25 files mirrored as 50
objects and read back. Plan A3 V2 GENERATED, FROZEN and MIRRORED:
`plans/generation_v2/a3/plan_a3_v2.jsonl`, **62,924 targets**, SHA-256
`a3e7df1929d3160158334de7c163b09bf5e644f6a7c353a6e377b532871dcf7b`, 6.34
GiB estimated, every one of the 166,075 solvable gaps reached, 156,531.6
expected months, all validations zero, byte-identical on a second run —
against the historical 61,941 / `53e3f0ca…`: **not reproduced** (+983
targets, digest differs; the historical rows are lost and the writer's
state at A3 time is unprovable, so the digest comparison is inconclusive
on its own while the count difference is real). Mirror 1,192 objects, 0
conflicts. B2.14R (verdict **C**): the +983 is not source drift — the B2.13
candidate window admits 10,257 filings from November–December 2021 that the
historical harness excluded (9L.1: 292,866 window filings / 269,521
candidates; V2 under the same window: 291,623 / 269,566). With only that
changed the unchanged planner gives 61,628 targets (−313, −0.5% vs
historical), packing 2.670 vs 2.673. Every other difference is bounded and
listed; the historical digest is a count-only comparator (93% of selections
are `(cik, accession)` tie-breaks). B2.13R restored the historical window:
`inputs.in_candidate_window` admits only `WINDOW_START <= filing_date <=
WINDOW_END`, `CANDIDATE_WINDOW_DEFINITION` `HISTORICAL_9L_V1`,
`A3_INPUTS_VERSION` `B2.13R/1`. **Corrected PlanningInputs FROZEN at
`plans/generation_v2/a3_inputs_r1/`, package SHA-256
`79f644f1f8af85422b68911ef5427e6150886294a8ae8b4da57a34e4c9a7622e`:
269,566 candidates (−10,206, all November–December 2021), 164,572
solvable gaps (−1,503 Jan/Feb 2022 → no candidate), floor 77,697, held
32,413; deterministic across two assemblies and a second invocation;
mirrored as 54 objects and read back. The B2.13 package `83680ead…` and
the B2.14 plan `a3e7df19…` (62,924) are SUPERSEDED_INPUT_ASSEMBLY /
NOT_AUTHORIZED_FOR_EXPANSION — kept locally and remotely with
`SUPERSEDED.json` beside each, never deleted. AUTHORITATIVE V2 PLAN A3:
`plans/generation_v2/a3_r1/plan_a3_v2.jsonl`, **61,628 targets**, SHA-256
`0efa7d038e063099dff86ff6e74ea9a2aab61feed62c45f29829511eba9b9507`,
6,599,925,841 bytes estimated, every one of the 164,572 solvable gaps
reached, 155,114.5 expected months, all validations zero, byte-identical
on a second run, `EXACT_FORENSIC_REPRODUCTION` of the B2.14R run 1
(61,628 / `0efa7d03…`), −313 against the historical 61,941 (count-only
comparator; the digest is not a gate); no target dated before 2022-01-01;
`authoritative_a3_plan.json` written (`AUTHORITATIVE_V2_A3_PLAN`,
`AUTHORIZED_FOR_EXPANSION`) with the supersession chain; 10 files as 20
objects mirrored under `control/…/a3-r1/` and read back; recovered from
B2 alone and re-planned to the same digest —
`A3_PLAN_RECOVERY_SUFFICIENT = YES`. B2.14R2: APPROVED_FOR_EXPANSION.
A3 EXPANSION COMPLETE: V2 baseline COMPLETE (38,146 trusted, manifest
`13722e02…`) + corrected PlanningInputs `79f644f1…` + authoritative A3
`0efa7d03…` (61,628) → **61,628 new A3 filings trusted, 0 failed**, every
one fetched from the SEC, PUT, read back and hashed before its manifest
row; final corpus **99,774 trusted** (submissions 5,733 · filings 94,041 ·
fulltext 0; 42,669,775,288 logical bytes); final manifest
`a5653f1ca065bddf50a8354faedde56df044f738efc6c0e92c17dd2cfc92c029`
(99,774 records; parent baseline `13722e02…`, snapshotted with stage
`A3_EXPANSION_COMPLETE`, the `BASELINE_COMPLETE` snapshot preserved); A3
terminal remote verification PASS (61,628 bodies, 0 mismatches); full
namespace 99,774 objects = 99,774 records, 0 orphans, 0 missing;
`a3_expansion_complete.json` `af88cc24…` mirrored; A3 recovery PROVEN
(`A3_EXPANSION_RECOVERY_SUFFICIENT = YES`). Historical V1 (61,941 /
`53e3f0ca…`) is historical only. POST-A3 DERIVED INDEX COMPLETE:
`derived/sec_evidence_index_v2_post_a3.db`, 344,674,304 bytes, SHA-256
`e6ce97d6bf8ecb1f3586fd3e7afa71a687a39bc7648fa3e45b877d181447abef` — the
verified B2.12 index (`3deb46b7…`, byte-unchanged and kept) plus exactly
the 61,628 A3 filings, each one GET verified against the manifest before
parsing, 0 parse failures; filings 94,041, securities 124,479, catalogue
716,290; the baseline's rows fingerprint as B2.12 recorded; fingerprints
filings `0aebbf30…`, securities `ee19167a…`, cover `b84800d4…`; marker
`post_a3_index_complete.json` `d91ff18d…` mirrored. ACTUAL PIT
MATERIALIZATION COMPLETE (`plans/generation_v2/post_a3_pit/`, two
identical runs): of the **242,269** months open before A3, **155,620
newly resolved (64.23%)**, **86,649 remain open**, 0 regressed; predicted
solvable 164,572 → 155,040 resolved (94.21%), 9,532 still open; predicted
unsolvable 77,697 → 580 resolved anyway (535 `G2_NO_CIK`, 33 `G3`, 12
`G4`), 77,117 still open; **expected solved 155,114.479 (diagnostic) vs
actual 155,620: +505.5, ×1.0033**; every resolved month has an A3 source,
0 future-invalid edges, 0 identity violations. Still open by class: G1
8,487 · G2 27,668 · G3 11,991 · G4 35,501 · G5 3,002. B2.17 RESIDUAL AUDIT
(`plans/generation_v2/pit_audit/`, two identical runs): of the 86,649
residual months, `SEC_MORE_MAY_HELP` 8,487 (9.8%; reachable by 12,320
unheld candidates; the unchanged planner would need 3,640 documents and
~2.0 GB at 2,332 months per 1,000 documents and 4,453 per GB, against A3's
2,525 and 30,225 — a FORENSIC_ONLY plan, NOT_AUTHORIZED_FOR_ACQUISITION),
`IDENTITY_DATA_REQUIRED` 41,004 (47.3%: no CIK 27,668, identity/ticker
ambiguity 6,338, class ambiguity 6,998), `NON_SEC_EVIDENCE_REQUIRED`
30,860 (35.6%; 25,689 residual months belong to 813 foreign 20-F/40-F
filers whose annual filings reach three month-ends a year),
`SEC_MORE_UNLIKELY_TO_HELP` 4,505 (5.2%; held filings in reach with no
usable cover), `STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL` 1,793
(2.1%; no listing observation). A3's final decile delivered 988 months per
1,000 documents and 5,406 per GB against deciles 1–7 at 2,800–3,100 and
53,000–86,000. **Verdict A: no further SEC acquisition justified — V2 SEC
EVIDENCE ACQUISITION FROZEN.** Further SEC acquisition: NOT JUSTIFIED. No
A4 exists. SEC/PIT EVIDENCE PROGRAM: CLOSED / FROZEN — generation
`SEC_EVIDENCE_GENERATION_V2`; final evidence 99,774 trusted (94,041
filings, 5,733 submissions), manifest `a5653f1c…`; final PIT 155,620
previously-open months resolved, 86,649 residual, 0 regressions, 0
future-invalid. RESEARCH DATASET: `PIT_RESEARCH_DATASET_V2`; BACKTEST
CONTRACT: `BACKTEST_DATA_CONTRACT_V1` at `contracts/backtest_v1/`,
mirrored under `backtest-contract/`, fail-closed on the universe, manifest,
index, PIT and audit markers, planning inputs, plan, database and the
dataset fingerprints; readiness verdict
READY_WITH_DOCUMENTED_LIMITATIONS with one blocker (sector/industry: current
labels only); first safe research class: monthly cross-sectional, signal at
the month's last close, execution at the next session's open. NEXT PROGRAM:
R1 — STRATEGY RESEARCH & BACKTESTING.** The frozen
resolution stands: 8,919
identities, 6,048 resolved (402 `STORED`, 5,646 `SEC_TICKER_MAP`), 2,871
`UNRESOLVED`; the ticker map was fetched once, in B2.10, and never again. See
[A3 planning inputs](a3-planning-inputs.md),
[B2.12](storage-layout.md#b212-the-derived-index),
[B2.11](storage-layout.md#b211-the-baseline-rebuild),
[B2.10](storage-layout.md#b210-the-rebuild-canary),
[B2.9X](storage-layout.md#b29x-the-second-generation),
[B2.9](storage-layout.md#b29-the-migration-plan),
[B2.8](storage-layout.md#b28-the-live-canary),
[B2.7](storage-layout.md#b27-the-backblaze-backend),
[B2.6](storage-layout.md#b26-backend-selection-and-the-b2-configuration-boundary),
[B2.5](storage-layout.md#b25-control-plane) and
[B2.2–B2.4](storage-layout.md#b22b24-storage-contract-checkpoint) in the
storage layout reference. The pre-refactor rollback point is `pre-b2-ssd-state`
(`13d586b10dfc077325b2729602a30b74d01a8af0`).

**Phases 1 through 7 are complete. All of them are frozen.** The MVP finished at
Phase 5; Phases 6 and 7 are layers over it, not revisions of it.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Ingestion, eligibility screen, universe | complete, frozen |
| 2 | Metric engine, CompounderScore, rankings, CLI, read-only API | complete, frozen |
| 3 | AI research — contract, provider, brief, pipeline | complete, frozen |
| 4 | Dashboard and watchlist | complete, frozen |
| 5 | Run control — jobs, ticker search, CSV export | complete, frozen |
| 6 | On-demand deep research (6A–6E: contract, prepare, collect, generate, dashboard) | complete, frozen |
| 7 | International coverage (7B–7F: currency, IFRS, cadence, FX, rollout) | complete, frozen |
| 8A | Market opportunity audit | complete, frozen |
| 8B | Point-in-time capture | complete, under review |
| 8C | Market signals | complete, under review |
| 8D | Point-in-time metric engine | complete, accepted |
| 8E | Data readiness — observation population and blocker audit | complete |
| 8F | Availability semantics and price depth | complete, under review |
| 8G | Point-in-time research outcomes | **complete, under review** |
| 8H | Backtest-readiness harness — survivorship, universe-as-of, score reconstruction | **complete, under review** |
| 8I | Historical securities — delisted names reconstructable from free EDGAR + Alpaca | **complete, under review** |
| 8J | Research dataset — backfilled signals and outcomes, coverage audit, identity guard | **complete, under review** |
| 9A | Identity foundation — stable `security_id`, dated identifiers, SEC fails-to-deliver evidence | **complete, under review** |
| 9B | Historical-security outcome integration — delisted names in the same outcome query | **complete, under review** |
| 9C | Historical point-in-time universe membership — dated intervals, three states | **complete, under review** |

**Phases 8B, 8C, 8F, 8G, 8H, 8I and 8J are implemented and awaiting review.**

Phase 8A was an audit, not an implementation: it established that the ranking is
not "dead" (Top 100 median ATR 4.01% against 3.36% for the universe) and that the
real blockers are measurement, not scoring. Its findings are in the Phase 8A
report; the ones that became requirements are recorded below.

Phase 8C adds the descriptive market-state layer over 8B's feature record:
peer percentiles, a multi-label setup, and a tri-state tradability verdict. It
produces no score, no ranking and no blend — Phase 8A falsified all three.

Phase 8D closes the last structural point-in-time gap: fundamental metrics can
now be built from what had actually been filed by a given date, reusing the
frozen normalisation and the frozen arithmetic unchanged. It adds no table and no
migration.

Phase 8G adds the outcome half: a market capitalisation correct on a past date,
forward returns at five horizons, and a delisting registry. It also produced the
measurement that now governs the phase — **across 4,599 companies and the 120
sessions after 2024-01-02, not one security stopped trading.** A universe that
size loses roughly a hundred names to acquisitions and failures over six months.
The zero is a property of `companies`, which holds what is investable today and
therefore cannot contain anything that stopped being so.

Phase 8H is the readiness harness that folds the point-in-time layers into one
audit. `stock_screener.backtest` answers, for a security on a date, the seven
questions a survivorship-aware study needs settled — universe membership,
filed fundamentals, point-in-time market cap, CompounderScore, signals, forward
outcomes and delisting fate — and names every gap it cannot fill. Its findings:
the CompounderScore is fully reconstructable from point-in-time inputs because
`score_company` has no cross-sectional term; delisting fate is classified as
`ACQUIRED`, `BANKRUPT`, `DELISTED`, `RECEIVERSHIP` or `UNKNOWN`
(`domain.DelistingReason`); and market-signal history is backfillable from stored
prices with no provider and no look-ahead. It adds no table, no migration and no
score version. See [Backtest readiness](backtest-readiness.md).
Phase 8I closes the last of those gaps: a delisted security is now reconstructable
from the free record that outlives its listing. `historical_securities` (migration
`0026`) is keyed by **CIK** — stable across a ticker change, distinct under a
reused ticker, separate for an acquiree and its acquirer — and is deliberately not
a `companies` row, so a delisted name is addressable without entering the
investable set. Its three child tables mirror the point-in-time company record
(observations selected on `filed_date`, daily prices, corporate actions), and
`stock_screener.backtest.reconstruct_historical_score` reads them through the same
frozen `build_company_metrics` and `score_company` the live path uses. A
representative cohort validates it end to end: ATVI reconstructs to a $65.5bn
market cap, a 53.9 score and a $95.00 terminal value; a delisted bank is declined
as `UNSUPPORTED_SECTOR`; and every gap the free data cannot fill — an FDIC-only
bank's fundamentals, a pre-2009 filer, pre-2021 prices, a receivership's terminal
value — is kept `UNKNOWN`, never guessed.
Phase 8J turns the mechanisms into a dataset. `stock_screener.backtest.backfill`
runs the *existing* daily passes over sessions that have already happened —
`capture_day`, `derive_market_signals` and `measure_forward_returns`, unchanged —
idempotently, resumably and one transaction per date, with a price cache that is
proven equivalent to the repository read field by field. `backtest.coverage`
audits the result as a funnel from "in the record" to "studiable without
inventing anything", and reports universe membership as `reconstruct_universe`
returns it rather than substituting a proxy for it.

It also fixed an identity defect the audit exposed. Delistings join to companies
by ticker, and a ticker is not an identity: `CZR`'s 2020 exit belongs to the
Caesars that merged into Eldorado, not to the `CZR` printing bars today.
`reconstruct_universe` was removing 127 live companies from every universe on
that basis, and `security_readiness` was reporting them as acquired. The price
record now adjudicates — a company still trading after an exit did not take it.
No score, signal or version moved; this is a data-quality fix in the audit layer.

**Measured on `stage9.db`, a throwaway copy of `stage8e.db`.** Forward returns:
1,064 daily decision dates over 2022-01-03..2026-03-31, **30,669,800 measurements**
across 5,765 securities, zero failed and zero missing. Market signals: 56
month-end sessions over 2022-01-31..2026-08-28, **282,998 signal rows**, zero
failed and zero missing; the full daily pass over 1,168 sessions runs at ~10s a
session. Historical securities: **402 ingested**, 400 with fundamentals, 402 with
prices, 291 with a stated terminal value, zero failures. The monthly coverage
audit reports **51 consecutive month-ends with a median of 1,698 fully usable
securities** (minimum 1,533, maximum 1,793), 2022-01-31 through 2026-03-31.

Look-ahead was checked rather than assumed: across 30.67M measurements, zero rows
have an entry on or before the decision date, zero have an exit on or before the
entry, zero carry a number without `REALISED`, and a 200,000-row sample confirms
every horizon spans exactly N benchmark sessions.

The phase's own finding is the one that governs it: **the historical universe
cannot be reconstructed at all.** Every company's membership record begins on
2026-08-26, the day capture first ran, so `universe_members_as_of` returns
*undetermined* — not baseline — for every earlier date, and the historical
universe is empty on every date in the study window. That is not a backfillable
gap. `companies` holds today's roster, so writing membership events for past
dates would assert that today's survivors were present then, which is the exact
survivorship bias the point-in-time record exists to remove.

Two further limitations were measured and are accepted rather than fixed. **The
dataset is censored, not survivorship-aware.** 3,801 securities left the market
over 2022-01..2026-08; the record holds **22** of those exits as observable
events (0.6%), because `companies` contains only what is investable today. The
delisted names that would close the gap have recoverable prices — the free
provider still serves them by ticker, verified on ATRS, SWIR, SIVB and BBBY — but
nowhere to be stored: `price_history` requires a `companies` row and
`historical_securities.cik` is `NOT NULL`, while the free record establishes a
CIK for only **12.5%** of delisted tickers (Form 25 full-text search, which is
additionally skewed to Nasdaq because only its exhibits embed the symbol).

**A third identity problem is present and not fixable in this schema.** 638 of
5,765 companies (11.1%) have a price series with a discontinuity longer than 120
sessions, and only 34 of those have a delisting recorded inside the span. The
proven case is `companies` row 2147: ticker `BBBY`, named *Neighborhood
Intelligence, Inc.*, carrying Bed Bath & Beyond's collapse to $0.0743 on
2023-05-02, a 26-month hole, then an unrelated company from 2025-07-14. One row,
two issuers, and no CIK on `companies` to separate them.

Strategy research is **not** started and is not to be started without being
asked. The binding constraint has moved but not lifted: the mechanisms are now
built and tested, but two of their inputs only accumulate with time — universe
membership is baseline-only until witnessed transitions land, and the *recorded*
score and signal series begin the day capture first ran. Reconstruction reaches
back over the observation record; the witnessed history does not yet exist.

### Data state

**The observation record is populated on staging.** A full-universe run on
2026-08-30 ingested **5,711,643 observations across 5,497 of 5,769 companies
(95.3%)**, with filing dates spanning 2009-04-15 to 2026-08-28 and zero undated.
2,632 companies carry ten years or more of point-in-time filing history. The
primary database did not receive it — as with Phase 7F, code and data promotion
are separate operations.

**Correction, found during Phase 8A:** the primary database *does* contain the
foreign cohort. GFI, ERO, DLO and ARIS all rank inside the current Top 20, and a
full `COMPOUNDER_V1_2` run is stored for 2026-08-26. The paragraph below was
written from the Phase 7 documents and was already stale; the database is the
authority.

~~**The primary database has not received the Phase 7F foreign cohort.**~~
`compounder_radar.db` is byte-identical to its pre-rollout state — the entire
Phase 7 validation ran against copies, verified by row count and checksum before
and after each stage.

Code completion and data promotion are separate operations. Promoting the cohort
is a deliberate, separate step: apply the migrations, then run the ordinary
passes — `update-eligibility-volume`, `enrich`, `update-fundamentals`, `score` —
against the real database.

**The outcome layer is populated on staging only.** `stage8e.db` carries 82,321
corporate actions across 5,769 companies, 5,796 delistings over 2020–2026, and
28,825 forward-return measurements from 2024-01-02. Market features and signals
were backfilled for that date to prove they can be: 4,651 of each, from price
data alone. `compounder_radar.db` has received none of it.

---

## Frozen decisions

### B2 evidence storage boundary

- Artifacts under `filings/`, `submissions/` and `fulltext/` use `EvidenceStorage`.
  Manifest JSONL and all other control-plane files remain local in B2.4.
- Definite absence is distinct from unavailable storage. Parser I/O failures
  now propagate intentionally, protecting PIT evidence from silent omission.
- Unsafe manifest keys are path violations only, never normal missing artifacts.
  Subset audits list touched artifact prefixes against the whole trusted set.
- **ETag is never evidence SHA-256**, including for any future backend.
- No object-store backend or evidence migration is part of this checkpoint.

### B2.5 control plane

- **The control plane has its own root.** `SEC_CONTROL_DIR` names where the
  manifest, checkpoints, plans, the reserved migration ledger and the derived
  index live; `EvidenceControlRoot` is the only way to reach them and it holds
  paths, never artifacts. Unset means the legacy colocated layout beneath
  `SEC_EVIDENCE_DIR`, which every command reports as a shared failure domain.
  The fallback is transitional.
- **Neither root is ever created.** An unreachable control root raises
  `ControlPlaneUnavailableError`; an unreachable artifact root raises
  `StorageUnavailableError`. Nothing conjures a directory on the boot disk to
  stand in for a volume that is not mounted.
- **Unwritten is not gone.** A manifest or checkpoint whose directory is present
  but which has not been written reads as empty or unattempted; one whose
  directory is missing raises. A vanished control plane never presents as a
  project that has done nothing.
- **The manifest records which artifact, never where.** `Entry.path` is an
  artifact-plane key; the same line is written whichever backend stored the
  bytes, and no control-plane operation lists or reads the artifact namespace.
- **Capacity is a local-backend question.** `EvidenceStorage` carries no
  free-space operation; `LocalEvidenceStorage.free_bytes()` and the
  `LocalCapacity` protocol do, and `preflight_corpus` asks only a backend that
  can answer. The corpus reconciliation gate runs against every backend.
- **`sec_evidence_index_v2.db` is rebuildable, not evidence.** The manifest plus
  the artifact corpus is the source of truth; the index is a local cache under
  `derived/` and is never mirrored. Only its small build metadata — bindings,
  table counts, semantic fingerprints, the index file's digest — is mirrored,
  so a later phase can prove which index it read (B2.12).

### B2.6 backend selection and credentials

- **The backend is selected, never inferred.** `SEC_STORAGE_BACKEND` is
  `local` or `b2`; B2 credentials do not choose `b2` and their absence does not
  choose `local`. Unset means `local` for configurations that predate the
  setting, and every evidence command reports that as a default.
- **A `b2` run never falls back to a local directory.** Incomplete or
  malformed B2 configuration is a `StorageConfigurationError` before any
  backend exists; a configured local root does not rescue it. The backend
  chosen for a run is the backend for the whole run.
- **B2 configuration is validated offline.** Presence, shape and coherence
  only — `https` endpoint with no embedded credentials, a bucket *name*, both
  credentials present. No authentication, HEAD or listing during validation.
- **Secrets stay in `Settings`.** `B2_KEY_ID` and `B2_APPLICATION_KEY` are
  `SecretStr`; no `repr`, log, error or status line carries their values, and
  status reports `configured`/`MISSING` only. Credentials come from the
  environment; there is no credentials file.
- **`b2` still requires `SEC_CONTROL_DIR`.** The legacy colocated fallback
  belongs to the local backend alone.
- **One construction point.** `open_evidence_layout` builds the backend;
  downstream code takes `EvidenceStorage`. The contract gains no B2 method,
  and ETag is never SHA-256.

### B2.7 the Backblaze backend

- **A bucket write is trusted only after a HEAD agrees.** `write_bytes`
  computes SHA-256 here, uploads it as `sha256` metadata, then requires the
  remote length and metadata digest to equal what was sent before returning a
  receipt. Any failure or mismatch is `EvidenceWriteError` and the manifest
  learns nothing.
- **`digest()` hashes the bytes it downloads.** Never the metadata claim,
  never the ETag; `x-amz-content-sha256` is transport, not evidence.
- **404 is the only "absent".** 403, throttling past the SDK's retries, 5xx,
  connection and read faults and a short body are `StorageUnavailableError`,
  from every operation including `exists`.
- **The object key is the logical key**, unprefixed, so manifests are portable
  between a directory and a bucket.
- **Nothing deletes, nothing lists buckets.** The `S3Operations` protocol
  names the four calls made; the application key needs `readFiles`,
  `writeFiles` and `listFiles` only.
- **The client is explicit.** Credentials from configuration, region from the
  endpoint, SigV4, path-style, 3 retries, 10 s connect / 60 s read, no
  ambient `AWS_*`. Single PUTs; multipart is refused, not improvised.
- **No live validation has been performed.** Every B2.7 test is offline.

### B2.8 the canary

- **The first live request is the canary's.** `evidence b2-canary --confirm`
  writes only under `_canary/<run-id>/`, through production wiring with a
  temporary control root, and deletes what it wrote. The production key
  mapping is unchanged; `CanaryNamespace` confines the real backend.
- **Cleanup is not a contract operation.** `EvidenceStorage` still has no
  delete; the canary's cleanup works on the raw client and refuses every
  prefix but exactly one run's.
- **Live tests are opt-in.** `live_b2` is skipped unless `--live-b2` is
  passed; `make check` makes no Backblaze request and needs no credential.
- **`NoSuchBucket` is unavailability.** A missing bucket raises
  `StorageUnavailableError` from read, digest and list — never an empty
  listing or an absent artifact.

### B2.9 migration planning

- **The manifest is the only source of trust for migration.** The plan is
  built from trusted entries under the manifest's replay semantics; a file on
  the old volume or an object in the bucket is never an input on its own, and
  trust is never reconstructed when the manifest is unavailable.
- **Migration reproduces trusted bytes.** `Entry.bytes` and `Entry.sha256`
  are the target; a refetch that returns different bytes is a
  `REFETCH_SHA_MISMATCH`, not a new artifact. The manifest is not rewritten
  because storage changed, and no second manifest is created.
- **Exactly one action per trusted key**, in a fixed precedence
  (`CONFLICT` > `REMOTE_VERIFIED` > local copy > `REFETCH_REQUIRED` >
  `SOURCE_UNRECOVERABLE`), and the plan reproduces byte for byte from the
  same inputs; source observations are inputs and are recorded as such.
- **The old volume is asked, never walked.** Manifest keys only, in order;
  the first fault ends the probe, keeps what was learned, and is not retried.
- **The bucket is listed, never written.** A remote object is verified by
  downloaded digest, never by metadata; an object no entry claims is a
  `REMOTE_ORPHAN` that blocks execution; `_canary/` is never evidence.
- **Planner classification and execution state are separate fields.**
  `Action` versus `ExecutionState` plus `FailureCategory`; never merged.

### B2.9X the second generation

- **Generation V1 is history and creates no trust.** The drive is permanently
  unavailable; the manifest, the corpus, Plan A3 and the checkpoints are lost.
  A number in a report is not a record. V2 starts at zero trusted artifacts
  and zero completed batches, and a checkpoint claiming completion over an
  empty manifest is a contradiction, not progress.
- **The universe survived exactly.** `stage9k.db`, read-only, reproduces the
  8,919 identities and digest `b8eaed3e…` through the same `production_universe`
  the V1 sweep used. That is the root of every V2 target.
- **The baseline is PARTIAL offline; Plan A3 is not offline at all.** Stored
  CIKs give 402 exact submissions requests; the rest need the SEC ticker map.
  A3's inputs were the parsed baseline corpus and its assembly harness was
  never committed; it is regenerated after V2.3 and *compared* to
  `53e3f0ca…`, never forced to match. Target reproducibility is not byte
  reproducibility.
- **Artifact keys are generation-independent.** `filings/<cik>/<accession>/
  <document>` is the same key in every generation and in the bucket; the
  generation id is metadata.
- **V2 keeps two copies of its control state.** A mirror of manifest,
  checkpoints, batch manifests and generation metadata after every completed
  batch is a hard requirement of V2.2, not an option.

These are load-bearing. Each is enforced by structure or by a test, not by
convention, and none may be relaxed without an explicit instruction.

### B2.10 the rebuild canary

- **No manifest row before the bucket has proven the bytes.** The V2 chain is
  fetch → SHA-256 → `write_bytes` (PUT, HEAD) → `digest` (GET, hashed) equal
  to the fetched digest → manifest append → `state.record`. The read-back is
  `harvest_artifacts(verify_written=True)`, a generic flag with no backend
  test in it. A read-back that differs raises before the manifest is
  touched; the object stays for reconciliation to find; nothing is deleted.
- **The control mirror is not evidence.** `control/SEC_EVIDENCE_GENERATION_V2/`
  is outside every artifact prefix; reconciliation never lists it and no
  mirrored file can be an orphan or a trusted artifact. Its trust is full
  read-back, an existing key is never overwritten (`MIRROR_HIT` or
  `CONFLICT`, and a conflict stops the run), and mutable files are mirrored
  as content-addressed bytes with a microsecond-stamped descriptor — never
  a replaced "latest".
- **A chunk is arithmetic, never a selection.** Chunk 000 is positions 0–249
  of the production universe order, frozen with its digest in the
  checkpoint; an unresolved identity stays in the chunk and contributes no
  request; a filer shared by several identities is one catalogue; filing
  targets are one per `(CIK, accession)`, `FILING_FORMS` only, newest
  first, cap 6, derived twice and byte-identical before anything is
  fetched.
- **Resolution is stricter than V1 and says so.** STORED → SEC_TICKER_MAP
  (live companies only) → UNRESOLVED. A delisted security without a stored
  CIK is not resolved from today's map even when its symbol is there; the
  declined count is recorded. The first generation asked the map for every
  symbol; the difference is reported, never corrected back.
- **A SEC refusal stops before the next request.** Targets go through the
  harvester one at a time; a `403` or a rate-limit failure raises at once.
  A `200` with an empty body is never stored. Anything still failed after
  a second pass stops the run; what succeeded stays trusted.
- **Chunk 001 is not authorised.** The command refuses any other chunk. The
  full baseline rebuild is a separate decision, taken on the canary's
  evidence.

### B2.11 the baseline rebuild

- **Chunks are built in order, one at a time, and a chunk is a no-op only
  when its mirror was proven.** Chunk N requires chunk N−1 `COMPLETE`. A
  rerun skips a chunk only if its checkpoint is `COMPLETE`, carries
  `recovery=proven`, and its frozen files hash to their companions and to
  the checkpoint; anything less runs again and converges as `CACHE_HIT`.
  Work is derived from the manifest, the checkpoints and the frozen plans,
  never from a report.
- **Verification is per artifact at write, per chunk in the replay, and
  whole-corpus once.** Every chunk downloads and hashes its own targets in
  the convergence replay and reconciles its own prefixes; every fifth
  chunk the namespace is walked at listing cost; after the last chunk
  every trusted artifact is downloaded and hashed once and the namespace
  fully reconciled. No chunk downloads an earlier chunk's corpus again.
- **Nothing in the bucket is ever overwritten or repaired by the run.** An
  untrusted target the bucket already holds is `REMOTE_ORPHAN`; a trusted
  artifact whose bytes no longer hash is `REMOTE_UNTRUSTED`, reported and
  left as found, never refetched (`harvest_artifacts(refetch_invalid=False)`
  is the generic form). Both stop the run for an operator.
- **The generation's resolution is frozen.** The preserved ticker map and
  the resolution snapshot are verified against their digests and against
  chunk 000's checkpoint before any chunk, and re-resolution that differs
  stops the run. The ticker map is never fetched again; the 8,919
  decisions — 402 `STORED`, 5,646 `SEC_TICKER_MAP`, 2,871 `UNRESOLVED` — do
  not change because the live map changes.
- **`BASELINE_COMPLETE` is written last and is deterministic.** Only after
  the last chunk, the terminal download-and-hash, the full reconciliation,
  the mirrored final snapshot and the bucket-only recovery proof; a rerun
  mirrors it as a `MIRROR_HIT`. Plan A3 is not regenerated by the baseline.

### B2.12 the derived index

- **One download is the verification and the parse.** The body fetched to
  be parsed is compared to the manifest's size and SHA-256 before the
  parser sees it; there is no separate digest pass and no HEAD, so a build
  costs one GET per trusted artifact. Bytes that do not reproduce the
  manifest are never parsed, inserted or ledgered — the build stops and
  names the artifact. `tests/integration/test_derived_index.py`.
- **The ledger is the resume state, and it is bound to the source.**
  `build_ledger` records every trusted artifact's key, expected digest,
  size and outcome (`COMMITTED`, `FAILED` with the reason, `NOT_INDEXED`
  for a kind the index does not consume); an artifact's rows and its ledger
  row commit together, in bounded batches, each artifact under a savepoint.
  `build_state` binds the build to the generation, the manifest SHA-256,
  the trusted counts, `PARSER_VERSION`, the schema version and the frozen
  input order, and a resume refuses on any difference. `--fresh` discards
  an in-progress build; nothing discards evidence.
- **The input order is frozen: catalogues, then filings, each by key.**
  Filings take their acceptance stamps from the committed `catalogue` rows,
  so an interrupted-and-resumed build and an uninterrupted one produce the
  same rows. Prefetching overlaps only the waiting.
- **A parser failure blocks completion; it is never missing evidence.** A
  catalogue that is not a JSON object, or a filing the cover parser raises
  on, is ledgered `FAILED` with its reason and the index is not promoted.
  A well-formed catalogue naming no qualifying filing, and a filing with no
  cover facts, are ordinary `COMMITTED` outcomes with empty derived rows.
- **Semantic fingerprints are the reproducibility contract, not file
  bytes.** SHA-256 over each table's rows in a total order — `catalogue`,
  `catalogue_stats`, `filings`, `securities` and the raw-evidence `cover`
  projection — excluding autoincrement ids and build timestamps. The index
  file's digest is reported as the digest of the artifact built.
- **Completion is written last and mirrored as a snapshot.** Only after
  every planned input is `COMMITTED`, `integrity_check` is `ok`, the
  structural checks pass, the fingerprints are computed and the file is
  promoted by one rename; `derived_index_complete.json` is deterministic
  (no timestamp) and, with the metadata and fingerprint files, is mirrored
  under `control/<generation>/derived/` as digest-addressed snapshots and
  read back. The index itself is never mirrored.
- **The same schema, the same parser, the same row meaning as V1.** No table
  was redesigned for V2; provenance lives in `meta`, `build_state` and the
  companion metadata file. Exact V1 row counts are diagnostic, not an
  acceptance criterion.

### B2.13 the A3 planning inputs

- **The planner's inputs are assembled, never the planner changed.**
  `sec_evidence/inputs.py` populates `plan.PlanningInputs` from surviving
  contracts — `join_symbol`, `build_reachability`, `supported_months`,
  `held_from_manifest`, the V2 index and the frozen universe — and
  `plan.py`, `reachability.py`, `join.py`, `coverage.py`, `sample.py` and
  `expand.py` are byte-unchanged. The command cannot run the planner
  (`tests/integration/test_a3_inputs.py::TestTheCommand`).
- **Two dated rules, each used where the code that owns it does.** A
  universe month is *classified* by a filing accepted by the month's first
  day (9K.7, read as frozen input); a month-end is *supported for planning*
  by evidence dated on or before it and within 90 days (9L). Held evidence
  is dated by its SEC acceptance date; an undownloaded candidate by its
  catalogue filing date, as `plan.Candidate` documents. The one-month
  boundary difference is reported (`covered_under_classification_rule`)
  and not reconciled.
- **Class-aware credit is the 9L.4 model, unchanged.** Observations are
  common-stock cover rows joined `EXACT` to an identity at their own
  acceptance date; issuer reach credits exactly the identities the record
  shows carrying common stock; `MULTI_CLASS_UNASSIGNED` and
  `IDENTITY_AMBIGUOUS` credit nothing; a sole tracked identity is
  `SAFE_SINGLE` whatever its held rows say, by the frozen rule. No identity
  is credited from CIK sharing.
- **A month no listing observed is not covered by anything.** Held class
  evidence for such a month is real and changes nothing; it is `G5_OTHER`,
  non-actionable, never `COVERED` and never in a candidate's `covers`.
- **The frozen universe is an input.** `ResearchUniverseMonth` is read,
  compared and never written; `class_source_accession` is compared in the
  dashed form the index uses and only in a diagnostic.
- **The package is bound to its sources.** `load_planning_inputs`
  recomputes every component digest, checks the package digest, and refuses
  a package frozen against another database, universe, resolution,
  manifest, index or fingerprint. B2.14 loads the package; it recomputes
  nothing.

### B2.13R the candidate window restored

- **A candidate is a catalogue filing dated inside the research window.**
  `WINDOW_START <= filing_date <= WINDOW_END`, both inclusive — the 9L
  definition that reproduces the 9L.1 inventory to within 45 candidates.
  This is acquisition scope, not point-in-time: a December 2021 filing is
  PIT-valid for January 2022 under the planning rule and is still not a
  candidate. Widening the window is a policy change under its own
  `CANDIDATE_WINDOW_DEFINITION`, never a quiet default.
- **A package names its window and is refused under another.**
  `candidate_window_definition` is a binding; `load_planning_inputs`
  refuses a package whose definition differs from the caller's. The manifest
  also records the bounds, the comparison semantics and `assembly_sha256`.
- **Supersession is additive.** A corrected package is written to a new
  directory and mirror prefix (`a3_inputs_r1` / `a3-inputs-r1`), carries a
  `supersession.json` naming the old package and plan digests, the forensic
  verdict and commit; the old artifacts gain a `SUPERSEDED.json` beside them
  and lose nothing, locally or remotely.

### R1.7 what building the runner settled

- **Capability is a code property, not a text one.** The order guard parses
  imports, attributes and call targets instead of grepping, so it cannot be
  fooled by a string and cannot be tripped by its own declarations. A shadow
  runner that merely does not call a broker today is one import away from being
  something else; one that contains no order capability cannot become one by
  accident.
- **Idempotency keys on what is stored, never on a fresh stamp.** A restart
  re-reads the activation and arm records. Comparing against a newly minted
  record would make the clock alone look like a second activation, which would
  turn every restart into a protocol conflict.
- **A derived date is a rule executed once, not a value written down.** The
  first eligible decision is computed at arming from the activation instant and
  the calendar, recorded with its cutoff comparison, and never accepted as
  input.
- **A durable arm record must predate the decision it vouches for.** Arming is
  written and mirrored before any capture, because a later claim that the runner
  was armed is worth nothing without a record that came first.
- **The observation count is derived from packets, never incremented.** A
  counter can be advanced twice; a count of the packets that exist cannot.
- **A frozen calendar is verified against the record it replaces.** One that
  cannot reproduce the sessions the research already used is not one to take a
  prospective observation under, and an unscheduled future closure is a data
  event the runner must fail closed on rather than a rule it can derive.
- **Sealing beats discipline.** Status reports the criteria as sealed rather
  than as a running total, so nobody has to decide whether to look at an interim
  reading.

### R1.6 what freezing the candidate settled

- **A candidate records why it was frozen, and why it was not.** H2 is frozen on
  its preregistered mechanism result. The performance edge is written into the
  same block as the reason, labelled noise, with
  `performance_entered_the_verdict: false` — so no later reader can mistake the
  return for the justification.
- **A prospective criterion is paired and directional, never a historical
  magnitude.** Requiring the prospective test to reproduce 28.23% would fit it to
  an effect size that came out of the data which generated the hypothesis. The
  claim is directional: less turnover, less churn, same exposure, safe execution.
- **A first observation date is a rule, not a date.** A protocol whose validity
  depends on when someone happened to deploy is not a protocol. The first
  eligible decision is derived at activation, and a quarter the runner did not
  capture live is skipped rather than reconstructed — a reconstruction is a
  backtest whatever it is labelled.
- **Protocol integrity is a pass condition, not a caveat.** P4 counts a
  retrospectively replaced decision packet as a violation, so rewriting history
  fails the test rather than being absorbed by it.
- **A live stream does not inherit a frozen dataset's contract silently.**
  `PROSPECTIVE_SHADOW_CONTRACT_V1` writes the bridge out, and names the
  asymmetry: historically a late filing was absent, prospectively it arrives and
  sits there, so the obligation becomes active rather than incidental.
- **Historical and prospective observations never share a namespace.** The
  stream is separate by construction, so provenance cannot blur later.
- **Shadow, paper and live are three different authorisations.** R1.6 authorises
  preparation for the first only. The ladder is recorded so that promotion is a
  decision someone has to make rather than a drift.

### R1.5 what executing H2 settled

- **A mechanism verdict must be unable to see performance, not merely
  uninfluenced by it.** `evaluate` takes turnover, exposure and safety and has
  no parameter a return could arrive in; performance is computed by a separate
  function after the verdict is written. Intent is not a control.
- **A preregistered specification is spent once, and is never edited.**
  `ExecutionLock` writes a durable marker before the run and refuses a second;
  the spec keeps its original digest, and results live in their own artifacts. A
  document that gains a result afterwards is no longer the document that was
  frozen.
- **Two strategies are only comparable if they share their inputs.** The
  calendar, cross-sections, scores and target counts come from the comparator's
  frozen ledgers, and the target-count sequence and the first selection are
  asserted against it at run time. A rebuilt decile count would have made C1 a
  comparison between two portfolios rather than two rules.
- **When two measurement bases exist, use the stricter and publish both.** C2's
  frozen threshold came from a share of all traded notional including terminal
  legs; this phase measures churn against the turnover metric's own numerator.
  The frozen figure is the lower, so it is the harder test, and both appear in
  `r1_5_turnover.json`.
- **A turnover reduction is only the claimed mechanism if it comes from the
  claimed place.** C2 exists so that a fall in total turnover cannot be credited
  to the hysteresis when it came from somewhere else, and C3 exists so it cannot
  be bought by holding fewer names or more cash.
- **An exploratory result stays exploratory however good it looks.** SUPPORTED
  here means the mechanism behaved as claimed on data that generated the
  hypothesis. It is not confirmation, and the blocks are renamed
  `LEGACY_*_BLOCK` so none can be mistaken for a holdout.

### R1.4 what preregistering H2 settled

- **A hypothesis states its aggregate rule.** H1 stated five criteria and no way
  of combining them, and R1.2 had to respect that absence after the fact. H2
  fixes `C1 AND C2 AND C3 AND C4` before execution. A frozen criterion set with
  no combination rule is an ambiguity, and it is not to be repeated.
- **A threshold with an undeclared component is a diagnostic, not a criterion.**
  1.95× stays in the record as `LEGACY_H1_TURNOVER_REFERENCE` and is never an H2
  pass condition. It is also never moved: retiring a bound and rewriting one are
  different acts.
- **A rule's parameters are structural where they can be.** H2's band is two
  selection widths, and its target count is R1.2's. Neither was chosen from
  R1.3's distributions, because R1.3's distributions are the outcome H2 will be
  judged against. Where a number cannot be derived structurally, it is not
  chosen at all — no prospective duration is fixed here.
- **Contamination is recorded, never engineered around.** Every month of the
  frozen dataset has been exposed to the research process, so no subperiod may
  be relabelled as an H2 holdout, and a historical H2 run is an
  `EXPLORATORY_HISTORICAL_TEST` however carefully it was preregistered.
  Independent confirmation requires data the generating process could not see.
- **A preregistration phase cannot price anything.** `membership_hypothesis`
  imports no simulator and no pricing path, proved by reading its own source at
  bind time. The experiment does not exist until R1.5 runs it once.
- **One run, then a new hypothesis id.** If H2's result is poor, the band, the
  frequency, the entry threshold, the weighting and the execution rule are not
  adjusted and re-run. Any variation is a new hypothesis, a new preregistration,
  and contaminated for the same reason this one is.

### R1.3 what the failed turnover premise settled

- **Turnover is one-way, denominated on pre-trade NAV at the rebalance open,
  and annualised by nominal frequency.** `Σ|notional|` over BUY and SELL legs
  ÷ `2 × pre-trade NAV`, then `mean × rebalances per year`. Terminal
  settlements are outside the numerator; notional is pre-cost. That is the
  definition C3's 3.90× anchor was computed under and the one the quarterly
  figure is compared against, so the criterion compares like with like. The
  matched comparator's elapsed-years annualisation is a second convention
  reported alongside, never substituted for the first.
- **A forensic phase cannot simulate.** `turnover_forensics` imports no
  simulator, and `simulation_reachable` proves it by reading the package's own
  source at bind time rather than trusting a loaded module. A non-empty result
  refuses the run. Explaining a result and producing one are different phases.
- **An availability change is not a score decision.** `CAUSE_BY_EXCLUSION` maps
  every cross-section exclusion to tradability, score readiness, feature
  freshness, execution availability or evidence timing, and maps **nothing** to
  `RANKING` — that label is reachable only by a row no exclusion applied to.
  Attributing all churn to the ranking would credit the score with decisions the
  universe made: 13.0% of exits and 24.4% of entries are availability changes.
- **A mechanism working as specified is not a defect.** `classify` returns
  `M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT` only on positive evidence — a figure
  that does not reproduce, a decomposition that does not close, or a broken
  accounting invariant. Trading that follows correctly from a changed target set
  is the strategy's mechanism whatever the verdict on it.
- **The floor is arithmetic on trades that already happened.** An exit must be
  sold in full and an entrant bought to its target, so removing every resizing
  trade while holding the executed selections fixed gives the least trading those
  selections could have required. It is not a strategy, was not executed, and is
  what makes C3 unreachable rather than merely unmet.
- **H1 is closed, and closing it settles nothing about the score.** C1, C2, C4
  and C5 were met and are untouched. `H1_MECHANISTICALLY_FALSIFIED` answers the
  question H1 asked — whether frequency alone would halve turnover — and no
  member of `H1_STATUSES` is confirmation.
- **No replacement parameter was chosen, deliberately.** R1.3 measured where
  entrants and exits sit relative to the cutoff and chose no hysteresis width, no
  buffer, no minimum holding period, no selected fraction, no position count and
  no cadence. These data are the outcome such a rule would be judged against;
  picking the width here would fit the parameter to the result. The recommended
  dimension is `MEMBERSHIP_STABILITY_OF_THE_SELECTED_SET` and the numeric rule is
  R1.4's to preregister.

### R1.2 the quarterly experiment and what it settled

- **The executor is held to the frozen specification, not to its own memory.**
  `EXPECTED_RULES` lists what R1.2 is about to apply and `bind_experiment`
  refuses unless every entry matches `r1_2_experiment_spec.json` verbatim. It
  also refuses a spec that tests more than one frequency, fraction, weighting,
  execution rule or cost level, that carries a sweep, that is already marked
  executed, or whose hypothesis was not accepted.
- **The comparison is the same sessions, not the same calendar.** A quarter's
  last benchmark session is also that month's last session, so every quarterly
  decision date is one of R1.0's monthly decision dates. The quarterly
  cross-sections are therefore **sliced from R1.0's frozen ledger** rather than
  recomputed, and the monthly comparator
  (`R1_0_MONTHLY_TOP_DECILE_MATCHED_WINDOW`) is R1.0's own NAV and trade
  ledgers restricted to the quarterly window. Nothing is re-simulated; a test
  asserts the comparator contains no simulation call.
- **No aggregate rule was invented.** The frozen specification states five
  falsification criteria and no way of combining them, so R1.2 reports each
  individually and reduces them only to the unavoidable all/none/mixed summary,
  recording in the artifact that this applies no weights and treats no
  criterion as decisive.
- **A failed premise is a result, not a prompt to re-specify.** Criterion 3
  failed on its frozen threshold and the threshold stands. No second frequency
  was tried, and none may be tried before R1.3 reviews why the premise failed.
- **2025-01…2026-03 is still not confirmation.** Every R1.2 artifact carries
  `independent_out_of_sample_confirmation: false` and
  `REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION`. No replacement
  holdout was manufactured by re-cutting the split.
- **Benchmark-relative return is called benchmark-relative return.** Not alpha,
  and not attributed to any sector: historical sector remains blocked and a
  test scans every module in the package for a sector reference.

### R1.1 the signal review and the one hypothesis

- **R1.0's outputs are immutable inputs.** `bind_r1_0` re-derives every R1.0
  fingerprint — document digests from the bytes, ledger digests from the parsed
  rows, the cross-section digest from its seven-field projection — and refuses
  on any mismatch before a single statistic is computed. R1.1 changed nothing
  in the R1.0 record and executed no strategy.
- **Two contrasts, both reported, neither alone.** `D10 - D1` is what a
  long-short book earns and is a fair summary only of a monotone profile;
  `D10 - cross-section mean` is what a long-only top-decile portfolio is
  exposed to. Every block publishes both plus the full ten-decile profile and
  its shape (`RISING`, `U_SHAPED`, `FLAT`). The second measure was added during
  R1.1 after the holdout's U-shape was observed; **the classification rubric
  was not changed to use it.**
- **The rubric precedes the diagnostics.** `review.RUBRIC` holds eight
  conditions written as code before the leave-one-out, concentration,
  persistence and cost results existed. Statistical significance is
  deliberately not among them, because the question is whether one further
  preregistered experiment is warranted — not whether the effect is
  established.
- **The already-observed interval is named as such.**
  `R1_0_HOLDOUT_STATUS: VALID_FOR_R1_0_PREREGISTERED_STRATEGIES` and
  `NEW_DERIVED_HYPOTHESES: HOLDOUT_ALREADY_OBSERVED`. No untouched historical
  holdout exists in the frozen dataset and none is manufactured by re-cutting
  the split. Genuine confirmation requires observations that did not exist when
  a hypothesis was frozen, a paper period under exactly its rules, or an
  independently frozen future extension.
- **A specification cannot exist without a hypothesis.** `experiment_spec`
  raises unless the hypothesis reached `ACCEPTED_FOR_ONE_EXPERIMENT`, so R1.2's
  rules cannot become a strategy somebody simply wanted to try. The spec fixes
  one frequency, one selection fraction, one weighting, one execution rule and
  one cost level, and carries falsification criteria fixed in advance.
- **Two sections are skipped rather than invented.** The score-change
  diagnostic is `SKIPPED` because the frozen system defines no canonical notion
  of a score change; the regime diagnostic is
  `REGIME_DIAGNOSTIC_NOT_AVAILABLE` because no frozen point-in-time market
  state exists. Sector remains forbidden, and market capitalisation is not used
  as a size control because the contract records it as a lower bound.
- **`MARKET_SIGNALS_V1` was documented, not tested.** Its output semantics are
  written down and three candidate future questions listed. No scalarisation,
  no combination with the Compounder score, no performance test.

### R1.0 the backtest harness and the first frozen baselines

- **R1 evaluates the frozen systems; it does not revise them.**
  `COMPOUNDER_V1_2`, `MARKET_SIGNALS_V1`, `ResearchUniverseMonth`, the PIT
  identity and availability semantics, price history and the forward-return
  labels are inputs to this phase and are unchanged by it. A weak result is a
  result, not a reason to move a rule.
- **A research run binds or refuses.** `backtest_harness.verify_bindings`
  re-hashes the research database, the contract file and the two frozen
  point-in-time files, then loads `BACKTEST_DATA_CONTRACT_V1` through
  `load_contract`, which refuses any binding that has moved. There is no
  `--force`, and a test asserts the parameter does not exist.
- **The research universe is read, never recomputed.** A security is in month
  `M` when the frozen universe holds the identity-month and either the Phase
  9K build classified it `ELIGIBLE` or the B2.16 materialisation records the
  month `COVERED`. One rule is deliberately stronger than the frozen files':
  the establishing filing must have been accepted **strictly before** the
  decision session, because coverage was computed against the calendar month
  end and an acceptance date says nothing about the hour.
- **`CLOSE_SIGNAL_NEXT_SESSION_OPEN`.** The signal is the close of the
  month's last benchmark session; the fill is the open of the next. There is
  no same-close execution, no intraday model, and the only future price a
  decision uses is the execution session's open — a test proves changing any
  later bar changes nothing at all.
- **The decision path reads no label.** `crosssection`, `portfolio` and
  `universe` do not import the forward-return layer; a static test holds it,
  and a canary changes a stored label and asserts every cross-section row is
  byte-identical.
- **Nothing is dropped silently.** Every identity the universe admits is a row
  carrying the stage it reached and why it stopped. A missing execution price
  is recorded as `NO_EXECUTION_PRICE` and never backfilled from a previous
  close, a later price or zero.
- **`TERMINAL_LEG_POLICY_V1`.** A stated consideration settles the position on
  its effective date; a reported wind-up settles at zero; an unknown terminal
  value is carried at the last observed close, flagged, and reported beside a
  floor NAV that values every such leg at nothing. A falling price is never
  read as a settlement.
- **The split and the preregistration precede the results.**
  `research_split_v1.json` is written before the first score is reconstructed;
  `r1_0_preregistration.json` is written and hashed before any performance is
  evaluated; and `first_holdout_access` refuses to record an access event
  unless the preregistration on disk hashes to what the caller presents.
- **No optimisation happened, and the record says so.** `variants_tried` is 1,
  `parameter_sweeps` is empty and `selection_criterion` is `NONE` on every run
  manifest and in the research ledger. No threshold, top-N, horizon, weight or
  cost level was searched; one cost sensitivity
  (`SIMPLE_10BPS_PER_SIDE_V1`) was preregistered because the repository holds
  no frozen cost convention.
- **No sector logic.** Sector history is `BLOCKED`, so no ranking, cap, limit
  or attribution uses it; a test scans every harness module for a sector or
  industry reference. The frozen `COMPOUNDER_V1_2` reads an undated
  classification internally and that is recorded per observation as
  `classification_is_current`, not removed.
- **`MARKET_SIGNALS_V1` is deferred, not used as a ranking.**
  `MARKET_SIGNALS_V1_REQUIRES_R1_1_HYPOTHESIS_DEFINITION`: it emits
  multi-label setups, a three-state tradability verdict and peer percentiles,
  and defines no canonical standalone scalar ranking. Its tradability verdict
  *is* the frozen readiness rule every portfolio here honours.

### B2.18 the research dataset and the backtest contract

- **The evidence program is closed.** `SEC_EVIDENCE_GENERATION_V2` is the
  last evidence generation; no baseline expansion, A3 continuation, A4,
  ticker-map refresh or submissions refresh for the frozen window, and no
  retrospective use of September 2026 information. Reopening acquisition
  takes an independently reviewed correctness defect. The residual plan
  stays `FORENSIC_ONLY` / `NOT_AUTHORIZED_FOR_ACQUISITION`.
- **Three names, three concepts.** `SEC_EVIDENCE_GENERATION_V2` (evidence)
  → `PIT_RESEARCH_DATASET_V2` (the frozen research dataset) →
  `BACKTEST_DATA_CONTRACT_V1` (the rules and digests a backtest binds to).
  None is overloaded for another.
- **A backtest binds or refuses.** `load_contract(expect=)` refuses a
  contract whose dataset id, contract version, universe, evidence manifest,
  post-A3 index, PIT marker, planning inputs, plan, audit marker, database
  digest or dataset fingerprints differ from the caller's, or whose files
  do not hash as its completion marker recorded. No research code may run
  against whatever database is present.
- **The contract states rules the code already enforces, and names their
  owners.** `as_of` is the close (`pit.decision_time`); availability is an
  acceptance instant for companies and `filed_date < as_of` for historical
  securities (a documented lag); execution is the next session's open;
  forward returns are labels; missing is not zero; identity is
  `(kind, identity_id)`; current exchange/sector/statement profile are
  `CURRENT_APPLIED_BACKWARD` and sector is `BLOCKED`. The contract adds no
  semantics.
- **Labels never enter features.** `domain.scoring`, `domain.signals`,
  `domain.metrics`, `domain.eligibility`, `stock_screener.scoring`,
  `stock_screener.signals`, `pit.metrics`, `pit.capture`,
  `backtest.scores` and `backtest.historical` may not import
  `domain.outcomes` or `ForwardReturnRepository`; a static test holds it and
  a canary proves a future bar moves the label and not the feature.
- **Every research run emits a manifest** (`backtest_run_manifest_schema.json`):
  dataset and contract ids, the fingerprints it bound, strategy version and
  commit, rebalance and execution rules, cost and terminal-leg assumptions,
  date ranges, parameters, variants tried and the selection criterion, the
  benchmark, output fingerprints. A result names the population it was
  selected from.
- **READY does not mean every strategy.** It means the first safe class —
  monthly cross-sectional, close signal, next-open execution, long-only,
  delisted names included with a stated terminal-leg policy — is explicitly
  defined and honest. Intraday, sector-neutral, consolidated-liquidity and
  regime-overlay research are not supported yet and the contract says so.

### B2.17 the residual audit and the acquisition freeze

- **V2 SEC evidence acquisition is frozen.** The final corpus is 99,774
  trusted artifacts under manifest `a5653f1c…`; the final PIT state is the
  B2.16 materialization over the post-A3 index `e6ce97d6…`. No further
  SEC filing acquisition is justified by the residual, and none is planned:
  the residual counterfactual (`residual_sec_forensic_plan.jsonl`, 3,640
  targets) is `FORENSIC_ONLY` / `NOT_AUTHORIZED_FOR_ACQUISITION` and never
  becomes an A4 by default. A future acquisition is a reviewed decision
  under its own inputs version.
- **A residual month is classified by the assembly's own reason, once.**
  `audit-pit` gives each of the 86,649 months one audit class and one
  sufficiency category derived from the reason the unchanged assembly
  states, plus diagnostic flags; `SEC_MORE_MAY_HELP` is granted only to a
  month an existing unheld, in-window candidate reaches under the frozen
  semantics — never to speculation.
- **Utility is participation and prefix union, never causal credit.** The
  A3 marginal curve is the union of resolved months supported by the first
  N targets in plan order; a document's utility is the months it
  participates in. No month is attributed to one document.
- **The remaining gap is mostly not a filings problem.** 47.3% needs
  identity data (historical CIK, security-class continuity, symbol
  occupancy), 35.6% needs evidence a qualifying SEC filing cannot supply
  within 90 days (foreign annual filers, funds), 2.1% the model refuses by
  design (no listing observation); 9.8% is SEC-reachable, on the declining
  part of the curve. Those numbers, not a threshold, are the decision.
- **The audit changes nothing.** `ResearchUniverseMonth`, the index, the
  PIT package, the plan and the planner are read only; the audit's rows
  are rebuildable; only its small summaries are mirrored.

### B2.16 the post-A3 index and the measured outcome

- **The final index is the verified baseline plus exactly the delta.** The
  B2.12 index is re-proven (digest, metadata, marker, integrity, every
  fingerprint) and copied; the copy is re-proven; only the delta is parsed
  into it. The B2.12 file is never opened for writing and the baseline
  corpus is never read again. Any disagreement is a conflict, never a
  reason to fall back to a full rebuild silently.
- **The delta is the manifest beyond the baseline ledger, and it must be
  the plan.** Missing, extra and duplicate identities are counted and any
  of them refuses the build; dates, listing order and modification times
  identify nothing.
- **One read is the verification and the parse.** Length and digest are
  checked against the manifest on the bytes the parser consumes; bytes
  that disagree yield no rows and no ledger entry. The extraction is
  B2.12's, unchanged.
- **Adding the delta changes no baseline row.** The B2.12 fingerprint
  queries over the non-delta rows must reproduce the B2.12 fingerprints
  before the index is promoted.
- **The outcome is observed, never fitted.** `materialize-pit` runs the
  unchanged B2.13 assembly over the post-A3 index and compares month by
  month with the frozen pre-A3 rows; `RESOLVED`, `STILL_OPEN` (class
  re-measured) and `REGRESSED` are all counted. The planner's 155,114.479
  is printed beside the observed count and gates nothing.
- **Attribution is the model's source rule; participation is a set.** A
  covered month's source is the assembly's newest common-stock
  observation; which other A3 filings fall inside the window is reported
  separately and never called a cause. Every realized edge is rechecked
  for the temporal and identity rules; a violation is a defect.
- **`ResearchUniverseMonth` is read, never written.** The materialization
  is a derived package under `plans/generation_v2/post_a3_pit/`; its small
  files are mirrored under `post-a3-derived/pit/`; the row files are
  rebuildable and are not.
- **No A4.** Measurement ends the phase; a further acquisition is a
  reviewed decision, not a consequence of residual gaps.

### B2.15 the expansion

- **Execution authority is the record plus exact digests.** `expand-a3`
  refuses a plan directory carrying `SUPERSEDED.json`, one without
  `authoritative_a3_plan.json`, a record not marked
  `AUTHORIZED_FOR_EXPANSION`, a record naming another plan, or a plan that
  does not hash to `--expect-plan-sha`. The newest file or directory is
  never authority. The superseded plan `a3e7df19…` is never executed.
- **Batches are frozen once, mirrored before the first request.** Plan
  order, 1,000 per batch, each with its content digest and a record
  binding it to the plan, package and authorization record; one digest
  over the set. A file that exists must be byte-identical: membership is
  never recomputed on resume.
- **The manifest learns of an artifact only after its remote body read
  back.** Per target: SEC GET → PUT → HEAD → body GET → equal digests →
  manifest append, in plan order. The PUT response, the HEAD, the ETag and
  the metadata digest vouch for nothing. The SEC side is sequential through
  the existing rate-limited client; only the bucket side runs bounded in
  parallel.
- **A failed target keeps its identity.** Retried in a second pass over the
  same targets, never replaced; a persistent `404`/`410` stops the run as
  verdict C naming the targets; a refusal (401/403/429) stops before the
  next request; a transport fault stops as B, resumable. No target is
  skipped silently.
- **An unrecorded object is adopted only as the SEC's bytes.** An object at
  a target's key with no manifest row is fetched again from the SEC and the
  remote body read back; equal length and digest → adopted without a PUT,
  and the row says so; other bytes → verdict D, nothing overwritten. A
  transport fault on the PUT or its verifying HEAD is retried, bounded,
  through the same question — is the object there, and is it the SEC body.
- **The baseline is inherited, not rehashed.** The final state binds to the
  B2.11 marker, the baseline manifest digest, the authorization record and
  the expansion delta; the terminal body verification covers the A3
  artifacts only. Structural reconciliation is a LIST, never a HEAD per
  object.
- **Filing identity is `(cik, accession)`.** Co-registrant copies are
  distinct keys and distinct rows; nothing deduplicates on the accession,
  the URL or the document digest.

### B2.14R2 the authoritative plan

- **A forensic expectation is a gate, never a target.** `regenerate-a3`
  takes the rows and ordered digest an earlier forensic run produced over
  the same inputs and classifies the production result
  `EXACT_FORENSIC_REPRODUCTION` or `FORENSIC_PRODUCTION_MISMATCH`; a
  mismatch stops the command before authorization or mirroring. The
  expectation is compared after the run and read by nothing in it; the
  planner, its configuration and the package are unchanged.
- **Authorization is a record written after every gate.**
  `authoritative_a3_plan.json` names the plan digest, rows and fingerprints,
  the package digest and its window definition, the count-only historical
  comparison, and the chain superseded package → superseded plan →
  forensic commit → correction commit → corrected package → this plan.
  `write_authorization` refuses a plan that did not validate, did not
  reproduce, or missed its forensic expectation. The record has no
  timestamp, so a rerun over the same plan mirrors as a hit.
- **Superseded artifacts are refused by code, not avoided by care.** A
  package or plan directory carrying `SUPERSEDED.json` is refused by
  `regenerate_a3` and `write_a3`; the mirror prefix follows the plan
  directory (`a3_r1` → `a3-r1/`), so the superseded `a3/` objects are never
  written to. The superseded plan `a3e7df19…` is never executed.
- **The authoritative plan must be recoverable from B2 alone.** With the
  local package, index, databases and assembly refused, the package is
  recovered from its newest control snapshots and the unchanged planner
  reproduces the mirrored plan digest. Recovery that needs the working tree
  is not recovery.

### B2.14R the forensic review

- **A reconstruction is checked against the counts it must reproduce.** The
  9L.1 record gives the historical candidate inventory (292,866 window
  filings, 269,521 candidates); a reconstruction that lands within 45 of it
  under one definition and 10,251 away under another has found the
  historical definition. The candidate window is `filing_date` within
  `[WINDOW_START, WINDOW_END]`; the B2.13 extended window is a discrepancy
  to be decided, not a source difference to be explained away.
- **The historical Plan A3 digest is a count-only comparator.** 58,707 of
  62,924 selections are tie-breaks on `(cik, accession)` among candidates
  that cover disjoint gaps, and the rank is in every row; the digest moves
  with any local change while membership does not. Row counts and identity
  fingerprints carry the comparison; `53e3f0ca…` never becomes a gate.
- **A counterfactual must correspond to one proven difference.** Run 1
  changed the candidate window and nothing else; the held-set runs were not
  run because the batch lists are lost and are reported as bounds. No
  combination was searched toward 61,941.
- **Review does not touch what it reviews.** The plan, the package and both
  databases were byte-identical after the review; the fix is proposed, not
  applied.

### B2.14 the regenerated plan

- **The planner is run, never tuned.** `regenerate_a3` loads the frozen
  package through `load_planning_inputs` — refusing a package whose sources
  moved — and calls `plan_coverage_first(candidates)` with no limit, the
  historical Plan A configuration. Nothing is reassembled (the command's
  tests disable the database, the network and the assembly), no parameter
  moves the count toward 61,941, and `plan.py` is byte-unchanged.
- **A plan is validated against the package it was planned from.** Every
  target is a frozen candidate, not held, unique on `(cik, accession)`, of a
  qualifying form, carrying its candidate's byte estimate, and covering a
  solvable gap; two runs must agree byte for byte. A plan that fails is not
  written.
- **A digest that differs is inconclusive; a count that differs is not.**
  The historical `53e3f0ca…` is SHA-256 over `write_plan`'s JSONL, and the
  writer survives — but its state when A3 was written cannot be proven and
  the rows are lost, so the byte comparison alone says nothing about the
  selection. The count and the identity fingerprints do. The comparison is
  reported either way; the historical plan is never labelled invalid.
- **The plan is frozen, not executed.** B2.14 ends at the mirrored plan;
  no filing is fetched until the difference has been reviewed.

### Cross-cutting

- **Missing is not zero.** A metric the data cannot support is `None` all the way
  through — model, column, CSV cell, JSON. `0.0` means the company reported zero.
  In scoring, a missing metric earns neither zero points nor full marks; its
  weight is carried by the other metrics in its own component, never by another
  component. (ADR-0007)
- **The score records its own rules.** Every snapshot carries a `score_version`
  and no comparison crosses versions. A curve, weight, cap, coverage minimum or
  ranking-admission rule means a *new* version — never an edit to an existing
  one — and `docs/reference/compounder-score.md` changes in the same commit.
  (ADR-0006)
- **A data-quality fix is not a policy change.** The test is one question: *does
  any company's number change?* Recognising a company the existing policy already
  excluded, or correcting a misread input, is not a version bump. Prove it with a
  test rather than asserting it.
- **Historical snapshots are never rewritten.** A company excluded today keeps
  the rows it earned under the rules of the day it was scored.
- **Not every company gets a number.** A bank, an ineligible security and a
  company with two quarters of history each get a stored row carrying the status
  that says why.
- **No metered provider gates the market.** The broad scan runs on Alpaca and
  EDGAR alone. FMP is optional enrichment. A `PRELIMINARY` ranking is a valid
  ranking. A provider failure or `429` must never prevent scanning, scoring or
  ranking. (ADR-0008)
- **The frontend calculates nothing.** Every score, subscore, metric and claim on
  a screen came out of the database through `stock_screener.api`.

### Phase 3 — research

- A brief is bounded by the `score_date` of the snapshot it explains and prefers
  the figures the score itself recorded.
- Nothing reaches the database without passing validation. The order is
  brief → draft → `validate_report` → persist; `save_report` takes a
  `ResearchReport`, which only validation builds.
- A provider failure is a `FAILED` report, never an exception that ends a run.
- **`D.` proves a filing exists; `X.` quotes what it says.** An `EXTRACTED` claim
  must cite an `X.` excerpt, and a number found in filing text supports only a
  claim citing the excerpt it appears in — never pooled across a brief.

### Phase 5 — jobs

- **A job is an existing command, never new pipeline code.** `JOB_KINDS` maps a
  key to a CLI argument list and is the entire allowlist. A caller picks a key,
  never an argument.
- **A job carries no result.** It answers "is it running, did it work" and
  nothing else.
- **One pipeline run at a time, and research beside it.** Pipeline kinds are
  mutually exclusive; `research` is guarded per ticker.
- **The job endpoints spend money and have no authentication.** They are only
  reasonable because the API binds localhost. `JOBS_ENABLED=false` is the off
  switch, and binding the API elsewhere without one is a mistake.

### Phase 6 — deep research

- **Web evidence never changes the CompounderScore.** `research.EvidenceKind` has
  no `W.` member; score-explaining sections accept `DETERMINISTIC` claims only;
  `DeepResearchReport` has no score field. All three are load-bearing.
- **Phase 3 is frozen and reused, never edited.** `deep-research` imports Phase 3
  types outright. Never add a member to a Phase 3 enum, never subclass a Phase 3
  class. `ResearchReport` and `DeepResearchReport` are unrelated types in
  different tables.
- **Deep reports append; they never overwrite.** `deep_research_reports` has no
  unique constraint, because a deep report is a dated investigation.
- **A deep report issues no instructions.** No BUY/SELL/HOLD, no price target, no
  position sizing — absent from the contract structurally.
- **A stage is an existing pass, never new pipeline code.** No second
  fundamentals engine, no deep-research metric, no second score.
- **Network first, assembly second.** Everything that fetches lives in
  `preparation`; `brief` only reads.
- **An unrecognised domain is rejected, not demoted.** The allowlist in
  `sources.py` is the whole policy and its default is refusal.
- **`X.` stays authoritative for filings.** A web copy of a filing the brief
  already quotes is rejected; commentary about one is not.
- **Collection is optional and never fatal.** `external=()` is a complete brief.
- **Nothing unvalidated is stored or served.**
- **An empty section says which kind of empty it is.** `NO_EVIDENCE` means the
  brief had nothing; `NO_VALID_CLAIMS` means nothing survived validation.

### Phase 8B — point-in-time capture

- **The operational tables stay mutable; the record does not.** `score_snapshots`
  upserts and `financial_snapshots` overwrites, because a screener must be able to
  re-score and to accept a restatement. The `pit_*` tables and
  `fundamental_observations` sit beside them and are never updated. Both layers
  are correct; conflating them is what makes a backtest dishonest.
- **A day is wholly captured or wholly absent.** The run row reaches `COMPLETE`
  in the same transaction as the snapshots. A failure rolls everything back and
  records a `FAILED` run carrying no rows.
- **`filed_date` is the availability date; `period_end` never is.** Point-in-time
  selection compares `filed_date <= as_of`. An observation with no filing date is
  excluded from selection rather than falling back to its period end.
- **A restatement appends.** Two filings of one period are two rows. Idempotency
  comes from a content fingerprint, not from a unique constraint over nullable
  columns, which SQL would let duplicate.
- **`BASELINE` is not an event.** A state seen for the first time is dated the day
  it was seen and asserts nothing about when it began. Only `OBSERVED` rows are
  transitions the system watched. No historical membership is ever invented.
- **A corporate action is never inferred from a price movement.** An unadjusted
  split and a genuine halving are the same bar. The table is created empty and
  stays empty until a source is wired to it.
- **Windows are strict.** A feature named `_60` used exactly 60 sessions or is
  `None`. A window that quietly shortens itself makes two rows of a time series
  incomparable while looking identical.
- **Features are structured values, never a composite.** No OpportunityScore, no
  ranking, no signal — Phase 8A showed the dimensions are anti-correlated and a
  scalar over them means nothing.

### Phase 8C — market signals

- **No composite, ever.** The dimensions are anti-correlated (volatility against
  distance-from-high, Spearman -0.58), so an averaged 0-100 score ranks highest
  whatever is mediocre at everything. There is no score column in
  `pit_signal_snapshots` and there must never be one.
- **No blend with CompounderScore.** A 50/50 percentile blend retained one name
  of the fundamental top twenty. The layers stay separate and a future strategy
  decides how to weigh them.
- **Tradability is three-valued.** `UNVERIFIED` is never collapsed into either
  neighbour. Missing data is its own answer.
- **Liquidity reads in one direction only.** Stored volume is a single venue and
  a strict subset of the tape, so clearing a floor proves a minimum and failing
  it proves nothing. No IEX-to-consolidated ratio is ever assumed.
- **Structural activity outranks volatility.** Flat-bar share, zero-move share
  and closing-band width are checked before anything else, because HBB carried a
  3.91% average true range while printing an unchanged bar on 59.9% of sessions.
- **Every threshold is provisional.** None is fitted to returns, because one day
  of history cannot support calibration. They live in a parameter object so a
  future version calibrates without touching the rules.
- **Signals read the feature record and nothing else.** Not `score_snapshots`,
  not `financial_snapshots`, not `companies` — that is what makes a past
  signal unable to drift.

### Phase 8D — point-in-time metrics

- **The temporal bound has exactly one home.** `available_as_of` selects
  observations by `filed_date`; everything downstream is indifferent to when it
  is called. `build_company_metrics` was already a pure function over injected
  periods, so no arithmetic changed.
- **The provider's normalisation is reused, never reimplemented.**
  `periods_from_facts_document` is shared byte for byte by the live path and the
  point-in-time path. A second normaliser would drift, and drift here would look
  like history rather than like a bug.
- **No new metric version.** The definitions are unchanged; only the source and
  its bound differ. Minting a version to record that nothing moved would devalue
  the mechanism `score_version` depends on. See ADR-0012.
- **Nothing is persisted.** A metric is a function of one company's own facts and
  can be rebuilt exactly from the immutable observation record, so a second copy
  would earn nothing.
- **`period_end` is never a proxy for availability**, at any level, with no
  fallback. An observation without a filing date is stored, counted, and never
  selected.

### Phase 8F — availability semantics

- **`as_of` means the close of that trading day**, 16:00 America/New_York, and
  the close follows daylight saving. Stated once in
  `stock_screener.pit.decision_time` rather than implied by a comparison
  operator.
- **Availability is decided by an instant, never by a date.** Measured on the
  full 5.78M-observation record: 61.0% of observations come from filings
  accepted after the close, so `filed_date <= as_of` reveals 3.5M of them early;
  39.0% were public before it, so `filed_date < as_of` would withhold 2.3M
  unnecessarily. Neither operator is a correct test — which is why the fix was
  a new table and not a changed character.
- **Where the instant is unknown, the filing waits.** The fallback requires
  `filed_date < as_of`. It can delay a filing by a day and can never reveal one
  early.
- **Raising the price window does not backfill.** The market pass resumes from
  each company's last stored session, so `update-market --since` is the only way
  to deepen existing history.
- **Feeds are never mixed in one series.** IEX and SIP disagree by 36x on volume
  and differ on the close, and `price_history` records no provenance, so a
  partial SIP backfill would create an invisible discontinuity.

### Phase 8G — research outcomes

**Migrations `0024` (`forward_returns`) and `0025` (`delistings`).** Both
additive, neither uses `batch_alter_table`.

- **A market capitalisation is anchored on the share count's date, not the
  valuation's.** AAPL split four-for-one on 2020-08-31 and did not file again
  until 2020-10-30; for those two months the newest available count is pre-split
  and the price is not. Anchoring on the valuation date applies no factor and
  values Apple at $557bn. `domain.pricing.market_cap_as_of` takes
  `share_count_date` for this reason, and two regression tests name the wrong
  anchor so reintroducing it fails.
- **A recovered price is a lower bound, never an estimate.** Splits are undone
  exactly; distributions are not, because `adjustment=all` removes them too.
  The residual is one-directional and roughly the cumulative distribution yield
  since the date — about 3% for AAPL over six years. Removing it needs a raw
  price series, which the provider serves and this project does not store.
- **The entry is the next session's open.** A snapshot computable at a close
  cannot be filled at that close. Close-to-close would pay the entry at a price
  the decision depended on.
- **The horizon is counted on the market's calendar**, taken from the benchmark.
  A security's own bars would let one that stopped trading appear to reach its
  horizon on schedule.
- **A security that stops trading has no return, and saying so is the point.**
  `INTERRUPTED` carries no number: -100% would libel every company acquired for
  cash, 0% would erase every failure, and omitting the row would rebuild the
  survivorship bias. `UNPRICED` is kept separate for a gap in our own record, so
  a count of delistings never counts our missing bars.
- **`delistings` is keyed by ticker, not `company_id`.** Giving each delisted
  security a company row would fix the join by putting thousands of untradeable
  tickers into the table the scanner, the rankings and the dashboard all read as
  the investable set.
- **Delisting discovery is by date range, not by symbol.** Asking about a symbol
  can only confirm the fate of one already known. The provider's inactive-asset
  list is not a registry: 18 of 20 well-known large-cap delistings appear in
  neither the active nor the inactive list, while the corporate-action record
  has all of them with terminal values.
- **Receivership is not covered.** SVB and First Republic appear under no type.
  Documented, not guessed at.
- **`build_company_metrics` gained `share_basis_close`, defaulting to None.**
  The default path is byte-identical to before, which is what allows a
  point-in-time caller to correct the market capitalisation without touching a
  live score. No score version changed; no company's number moved.

### Phase 8I — historical securities

- **Identity is the CIK, never the ticker.** *Superseded by Phase 9A*, which
  found the availability wall: the free record establishes a CIK for about an
  eighth of delisted symbols, so `cik NOT NULL` made the table unable to hold the
  names it exists to hold. The identity is now `historical_securities.id`; the CIK
  remains stored and unique where it exists. The reasoning that a ticker is not an
  identity is unchanged and is what Phase 9A generalises.
- **A historical security is never a `companies` row.** `companies` is the
  current investable universe; the historical layer is separate tables with no
  foreign key to it, so the two sets cannot contaminate one another and a
  reconstruction cannot read today's company state — a delisted name has none.
- **Activity is decided by the date, not by today.** A historical security is
  eligible only on a date at or before its last trading session; the live path's
  current `is_active` flag is a look-ahead for a delisted name and is closed here.
- **The frozen machinery is reused, never reimplemented.** Fundamentals go through
  `periods_from_observations`, the market cap through `as_traded_price`,
  eligibility through `evaluate_eligibility`, the score through `score_company` —
  the same functions the live path calls. A test asserts the two paths produce an
  identical score from identical data.
- **UNKNOWN is preserved wherever free data cannot establish the truth.** An
  FDIC-only bank's fundamentals (absent from EDGAR), a pre-2009 filer (no XBRL),
  pre-2021 prices (beyond the free feed's reach) and a receivership's terminal
  equity value (in no feed) are recorded as gaps and left NULL — never a guessed
  zero. A receivership takes its status only from asserted evidence, never the
  action feed.

### Phase 9A — security identity

- **The identity is an opaque internal id, and every external handle is a dated
  interval against it.** A ticker is a lease, a CUSIP is a dated identifier of an
  *issue* remade by a reverse split, and a CIK exists for a minority of delisted
  names. None of the three may be the key: keying on a ticker splices two
  companies into one history, keying on a CUSIP splits one company's history in
  two, and requiring a CIK stores neither.
- **CUSIP is never promoted to the primary key.** It is stored as a dated
  identifier — `(security_id, cusip, ticker, venue, valid_from, valid_to, source,
  confidence)` — and a reverse split closes one interval and opens another
  against the *same* `security_id`.
- **The CUSIP issuer base is a rule, not a heuristic.** The standard reserves
  characters 1–6 for the issuer, so two issues sharing them are one company's and
  two that differ are two companies'. No lineage is ever drawn across that
  boundary, which is what keeps `BBBY`'s two occupants apart.
- **The issuer holding a symbol on a date is the latest to have taken it up by
  then** — the same rule `universe_members_as_of` uses for membership. *Corrected
  in Phase 9B.* The original test asked whether a span had begun by the date and
  stopped being seen shortly after it, which bounds a span's end from above and
  never from below, so any long-dead earlier occupant also qualified: `ATVI` came
  back ambiguous between Activision Inc (last seen 2008) and Activision Blizzard,
  and `TWTR` between Tweeter Home Entertainment (2007) and Twitter. Resolution
  went from 2,410 to **3,144** symbols and ambiguity from 680 to **14**.
- **A positive fails-to-deliver match is evidence; an absence is not.** A
  security appears in the SEC's file only when a delivery failed, and before
  2008-09-16 only above a reporting threshold. `domain.EvidenceVerdict` has
  exactly two members, `PRESENT` and `UNPROVEN`, and **no `ABSENT`** — so no code
  path can record the opposite conclusion even by accident.
- **Nothing is fabricated.** No CUSIP, CIK, ticker or issuer mapping is invented,
  and no interval endpoint is interpolated: every `valid_from` and `valid_to` is a
  settlement date the record actually carries. Evidence that fits two issuers
  produces `AMBIGUOUS` and creates nothing.
- **Evidence is stored apart from conclusions.** `ftd_identity_observations`
  holds evidence for symbols that resolved to nothing, which is exactly what an
  audit of what is missing is made of. An interval presupposes an identity;
  evidence presupposes none.
- **The two identity spaces stay separate.** Nothing in the identity layer writes
  to `companies`. Contaminated live rows are **detected and reported, never merged
  or split** — re-attributing a row's prices, filings and scores between two
  issuers is a data operation requiring its own authorisation. No `cusip` column
  was added to `companies`: there is no authoritative free source for a live
  company's CUSIP, and deriving one from the fails record means joining on the
  ticker, which is the contamination being detected.
- **A forward return names exactly one identity.** `company_id` or
  `historical_security_id`, enforced by a check constraint, with uniqueness held
  by two partial unique indexes — one per identity kind. A single constraint over
  a nullable pair would enforce nothing, SQL holding every NULL distinct from
  every other. **No existing measurement was migrated**; attaching a delisted
  name's outcomes needs its identity proven first.
- **No score, signal, threshold or ranking rule moved.** `COMPOUNDER_V1_2` and
  Market Signals V1 are byte-unchanged and their parity tests are untouched.

### Phase 9B — historical-security outcomes

- **A delisted security's outcome is a row, not a dataset.** `forward_returns`
  carries `company_id` **or** `historical_security_id`, so an ordinary query over
  a decision date returns both kinds. There is no `include_delisted` flag and no
  `UNION` for a caller to remember — which matters because the failure mode of
  every alternative is silent: a forgotten union does not raise, it returns a
  tidy survivor-only answer.
- **The measurement is the company path with one substitution.** The same
  `domain.forward_returns`, the same benchmark calendar, the same four statuses,
  the same table; only the bars come from `historical_prices`. The calendar stays
  the benchmark's, so a security that stopped trading is `INTERRUPTED` rather
  than appearing to reach its horizon on time.
- **Phase 9B adds no schema.** No migration, table, column, index or constraint —
  `0026` and `0027` were already sufficient. Terminal outcomes are **derived on
  read** for the same reason `classify_delisting` is: a stored second copy could
  only disagree with the columns it was derived from.
- **A ticker-keyed price fetch is bounded to the security's own era.** Prices come
  from a symbol-keyed feed, which is where the identity model could be undone by
  its own plumbing. Every request spans `[earliest identifier interval, last
  trading date + 5 days]`, so a reissued symbol cannot pull a later occupant's
  bars into this security's series.
- **A terminal event is matched by identity, never by symbol.** A security's exit
  is the delisting falling inside one of *its* identifier intervals. VMware has
  three rows under `VMW`; only the intervals say which is this security's, and the
  one stating a consideration is preferred over the ones that do not.
- **A price is never evidence of a terminal value.** `domain.terminal_outcome` is
  handed reported considerations and no price series, so a collapsing tape cannot
  become a settlement. Bed Bath traded to $0.0743 and its terminal value is
  `UNKNOWN`; every receivership is `UNKNOWN`, because no feed files one.
- **Price availability and score availability are separate.** A CIK-unresolved
  security has no EDGAR fundamentals and is `INSUFFICIENT_DATA`, and keeps its
  prices, its identity and its outcomes regardless. `CompounderScore V1.2` was
  not widened to reach it, and is byte-unchanged.

### Phase 9C — historical universe membership

- **Identity, membership and eligibility are three concepts, not one boolean.**
  Identity is `security_identifiers`; membership is a slow-moving listing fact
  stored as dated intervals in `universe_membership`; eligibility is the frozen
  threshold screen applied per date. Collapsing any two is how a universe becomes
  today's roster wearing a date.
- **No `MEMBER` verdict can currently be `OBSERVED`, and the schema says so per
  row.** Exchange, security type and sector are undated everywhere in this
  system — every stored exchange carries the day capture first ran. A member
  verdict therefore carries an attribute backwards, `confidence` records that it
  did, and `inferred_inputs` names which. `ListingAttributes.fully_dated` is the
  single gate on promotion, and becoming observed requires a dated venue source
  rather than a change of opinion.
- **Price presence is never membership.** Price and settlement evidence bound an
  interval; the membership claim rests on the listing attributes. A security with
  a full price series and no recorded venue is `UNKNOWN`.
- **`UNKNOWN` is a first-class state and is always reported.** Every security
  lands in exactly one of members, non-members or unknown — a universe that loses
  securities between its groups hides its own size, which the first
  reconstruction did for 1,530 of 8,919 securities before it was caught.
- **The record's own reach is not evidence about the market.** `NOT_YET_IN_RECORD`
  is only asserted when a security's first dated trace is strictly after the
  observation record's floor; at the floor the period stays `UNKNOWN`.
- **Membership is dual-identity, like outcomes.** `company_id` or
  `historical_security_id`, exactly one, so `members_as_of(D)` returns delisted
  securities without an `include_delisted` flag.

### Phase 7 — international

- **The filing's unit is authoritative, never the vendor's.** `reporting_currency`
  comes from the XBRL unit key; `quote_currency` is what the listed share trades
  in. One field held both and whichever provider wrote last won — mixing TSM's
  TWD statements with its USD market cap yields an EV/Revenue of 0.38x against a
  true 24.07x.
- **Convert the market side, never the statements.** Only the market
  capitalisation crosses currencies. Every ratio reads
  `CompanyMetrics.market_cap_for_ratios` — converted where a rate exists, `None`
  where one does not, never the unconverted figure.
- **A rate belongs to a date and a source.** `fx_rates` stores one row per
  `(base, quote, rate_date, provider)`. Rates are bounded to five days before the
  score date, never after it. `FX_UNAVAILABLE` is a warning, not an exclusion.
- **Coverage is never bought with a fabricated period.** Four annual periods are
  not a trailing year; six-month figures are never halved into quarters.
- **A share count that cannot be multiplied by the price is not read.**
  `MarketCapSource.CALCULATED` is never produced for 20-F/40-F filers.
- **A score built on `STALE` fundamentals keeps its number everywhere the number
  describes the company, and is excluded from current rankings.**

---

### Phase 9K.1 — free listing evidence

`listing_observations` records where a security traded, month by month, from
Nasdaq's public *Market Share by Symbol* files. Four rules govern it.

- **The `Listed*` family changed meaning and the code must know it.** Tape B
  (Arca, NYSE American, regional) through 2021-12; Tape A (NYSE) from 2022-01,
  with 2021-07 an exception inside the earlier era. `domain.tape_for` predicts a
  tape from the month, `domain.classify_tape` reads one from the file's own
  contents, and only a file where the two agree is imported. The rest are
  `QUARANTINED` rows carrying both tapes and the reason.
- **Absence is never non-membership.** `ListingEvidenceState` has no
  `NOT_LISTED` member, so no caller can write one. A security that did not trade
  in a month is simply missing, and 34.6% of observed symbols have a hole inside
  their own run.
- **Monthly is never daily.** `month_state` may return `MARKET_SHARE_OBSERVED`;
  `daily_state` never does, because the file attests to a month and a security
  that left on the 5th appears in it exactly as one that traded all month.
- **NYSE American is censored, and counted.** It appears in no importable file.
  247 live companies are stored on it; the audit reports them rather than
  treating them as absent.

Scope is **2022-01 onward** — the first month Nasdaq and NYSE are both covered.
Nothing before it is imported.

### Phase 9L.5 — the repaired planner, validated on fresh data

A representative 1,008-filing batch was harvested and measured against a
prediction frozen before any of it was downloaded. **actual / yield-adjusted
expected = 0.979** — inside the 0.90–1.10 gate, against v1's 0.412 on the same
kind of test.

Acquisition was clean in a single pass: 1,008/1,008 downloaded, 1,010 requests
(two 503s retried), no recovery pass needed, and a replay of the same manifest
issued **0 HTTP requests**. The corpus grew by exactly +1,008 trusted filings to
40,163 / 34,392, every integrity category zero, 1,008 new artifacts rehashed and
1,957 prior artifacts sampled, all clean.

What the batch was built to test, it tested. The dominant single-identity
stratum — 95% of Plan A3 — landed at **1.004**. The rank deciles are flat
(D1–D3 0.950, D4–D7 1.002, **D8–D10 0.985**), so calibration does not decay in
the low-ranked targets that make up the tail of the plan; years run 0.950–1.000
with no temporal bias; live 0.982 against delisted 0.903. **Zero future leakage,
zero incorrect cross-class joins, zero CIK-sharing over-credit.**

Two things needed care rather than acceptance.

- **The multi-CIK stratum reads 0.640 as frozen and 0.876 once corrected.** The
  frozen gap set predates batch 1's parse, and batch 1 over-represented
  multi-class filers 19x — so it had already closed 55 of the 203 months this
  batch was credited with. No download supplies a month twice. Both figures are
  reported; the corrected one meets the ≥0.80 guide, and the residual decomposes
  to already-covered 51, no-cover-row 24, subset-naming 9, with nothing
  unexplained. The traces confirm the repair holds where it matters:
  `HEI`/`HEI.A`, `GTN`/`GTN.A`, `KELYA`/`KELYB`, `SENEA`/`SENEB`, `MKC`/`MKC.V`
  and `UHAL`/`UHAL.B` are all genuine dual common classes, each credited *and*
  delivered.
- **The held-set source was wrong and is now the manifest.** `held_keys` read the
  derived index, a snapshot that lags the corpus; that is what let 9L.4 plan 800
  filings already on disk. `held_from_manifest` replaces it in the CLI, and a
  `--replay` flag makes re-running a completed manifest a deliberate act rather
  than something the overlap guard has to permit by accident.

Plan A3 is unchanged and now 1,008 targets closer: **60,933 remaining**, verified
by `(CIK, accession)` set arithmetic rather than subtraction, 6.53 GiB,
calibrated expectation 152,076 months. The final 1,941 targets beyond the 60k
knee buy 1,941 safe months for 1.28 GiB — 0.15% of free space — so they are
kept.

### Phase 9L.4 — separating issuer reach from security coverage

The planner is repaired structurally rather than statistically, and revalidated
against two independent samples with no network access.

The confusion was between two questions that share a filing but not an answer:
*can we obtain this?* (`(CIK, accession)`, issuer level) and *what may it
support?* (`(identity, month-end)`, security level). `sec_evidence/reachability.py`
answers only the second, from evidence that already exists.

The rule is not "distrust multi-identity issuers" — that would be a second wrong
answer of the same shape, since `LSXMA`/`LSXMB`/`LSXMK` are three genuine Liberty
tracking classes. A filing is credited with exactly the identities the record
already shows carrying common stock; where several identities share an issuer and
none is yet known to be the common one, the credit is **zero**, not a share. Of
264 multi-security CIKs: 191 resolve to exactly one common identity, 58 carry
more than one real common class, 15 remain unassigned. 312 identities the old CIK
rule would have credited are now excluded, and `AIZ`/`AIZN` credits one and
excludes one.

Both revalidations pass on their large samples. Against the 800-document batch:
raw ceiling 2,891, expectation 2,679, actual 2,544 — **0.950**, with the
multi-class stratum at **0.947** where v1 scored 0.397. Against an independent
3,000-document holdout drawn from the pre-existing corpus (SHA `91b9ef4b…`),
simulated by removing those observations from planner-visible evidence:
**0.940** overall and **0.951** single-identity.

Two things the measurement forced.

- **The raw prediction is a ceiling, and the expectation needs a measured rate.**
  The residual decomposes with nothing unexplained: 81.7% is filings carrying no
  usable common-stock cover row, 12.4% joins that are not `EXACT`, and only
  **5.8%** the remaining subset-naming effect. So the expectation multiplies the
  ceiling by a measured usable-common-row rate per form (8-K 92.2%, 10-Q 90.8%,
  10-K 85.8%, 20-F 57.9%) — computed with the holdout excluded, and materially
  lower than the 9L.1 any-symbol yields it replaces.
- **The candidate pool must exclude what is already held, from the live
  manifest.** The derived index predates the 9L.3 harvest, which makes it the
  right snapshot for *evidence* (no leakage into its own prediction) and the
  wrong one for *acquisition*. A batch-2 overlap assertion caught v3 planning 800
  filings already on disk.

`expansion_plan_v3` — 61,941 targets, 6.6 GiB, 165,553 safe months, 152,381
expected, 70.6% `UNKNOWN` reduction. The repaired gap model moves 6,717 months
out of solvable into `G3_IDENTITY_AMBIGUOUS` (8,016) and
`G5_MULTI_CLASS_UNASSIGNED` (702); the floor rises to 76,029 (29.4%). A knee now
exists: 60,000 targets reach 98.8% of the gap for 5.4 GiB. 8-K remains
overwhelming at 68,897 months/GiB against 10-Q's 1,429.

Plan status: **v1 superseded** (CIK-sharing over-credit), **v2 provisional** — a
structural recomputation from observed reach, not a haircut, but not the
class-aware model either, and its rows carry a `plan_version` of `v1` from a
stamping defect — **v3 the structurally repaired candidate**, not authoritative
until a representative network sample validates it.

### Phase 9L.3 — the planner was wrong, and the validation caught it

The 800-target validation batch was acquired, verified and measured. Acquisition
is sound; **the planner's coverage arithmetic was not**, and the batch existed
precisely to find that out before 61,984 filings were fetched on a false premise.

Acquisition passed cleanly. All 800 targets reached a terminal state, the corpus
grew by exactly +800 trusted artifacts and +63,954,126 bytes, every error
category reconciled to zero, and a third replay of the same manifest issued
**0 HTTP requests** — exact-manifest convergence. Storage came in at 0.0596 GiB
against 0.08 estimated, with the 8-K mean within 3% of the 9L.1 prior.

The defect: the planner credited a filing with covering **every identity sharing
its CIK**. Those secondary identities are baby bonds, preferred stock and
depositary shares. CIK 1042046 (`AFG`, `AFGB`, `AFGC`, `AFGD`, `AFGE`) yields 16
common-stock rows and 64 `NOTE_OR_DEBT`; CIK 7323 yields no common-stock row at
all. Measured: 2,238 identities assumed reachable, **784 actually reached**.

The split is decisive. Targets on single-identity CIKs realised **98.3%** of
prediction; targets on multi-identity CIKs realised **37.8%**. The identity layer
was never at fault — it correctly refused 1,821 non-common rows and made 842 safe
common-stock joins, with **zero future leakage** and correct dated-occupant
attribution for `LSXMA`/`LSXMB`/`LSXMK` and `PARA`/`PARAA`.

Two rules follow.

- **A filing covers an identity only where that CIK's cover pages actually yield
  a common-stock row that joins to it.** Never inferred from CIK sharing. The
  corrected model predicts 2,891 for the batch against 2,544 observed — 1.14x,
  where the original predicted 6,402 — 2.52x.
- **A stratified validation sample is not a representative one.** The batch was
  built to over-weight multi-class filers and does so by **19.3x** (92.6% of its
  targets versus 4.8% of the candidate pool). Its 39.7% realisation is a
  worst-case stress result, and reading it as the plan-wide expectation would be
  as wrong as the defect it exposed. Across the whole pool the correction costs
  5.1%, not 60%.

`expansion_plan_v2` (61,007 targets, 6.2 GiB, 163,413 calibrated months, 70.5%
`UNKNOWN` reduction) is proposed alongside v1, which is left untouched.

### Phase 9L.2 — the selective expansion plan

"Expand selectively" now has a number. **61,984 filings and 6.4 GiB close 100% of
the solvable gap**, against 269,538 filings and ~150 GiB for the naive
fetch-the-whole-window approach — 77% fewer documents and 96% less storage for
identical coverage. Nothing was downloaded.

The gap was classified before anything was planned, because the distinction
between gaps filings can close and gaps they cannot is the whole decision. Of
258,463 `UNKNOWN` month-verdicts: **172,270 (66.7%) are solvable**, 36,661
(14.2%) have no resolvable CIK, 30,070 (11.6%) have a CIK but no catalogue filing
that could cover them, 606 (0.2%) are identity-ambiguous, and 18,856 (7.3%) are
already covered. The floor is therefore **67,337 `UNKNOWN` (26.1%)** — a 73.9%
reduction is the ceiling, and no budget moves it.

Three findings shaped the plan.

- **8-K carries the phase.** Alone it reaches 98.0% of the solvable gap for 2.4
  GiB across 60,701 filings. 10-Q reaches 70% for 81.3 GiB; 10-K 27% for 40.2
  GiB. The 8-K population is genuinely useful rather than merely numerous: 42 KB
  mean, 96.4% observed cover yield, and event-driven timing that spreads through
  the year where periodic reports cluster.
- **Coverage-first greedy run to exhaustion *is* the interval cover.** Plan A and
  the minimum-temporal-cover plan return the same 61,984 filings, because each
  selection re-scores against what is already covered and a filing whose months
  are wholly taken contributes zero. They are reported as one result, not two.
- **The co-registrant duplicate is real and now explained.** 9L.1's "32,601 vs
  32,584" was an artifact of matching on accession alone: 17 accessions are held
  under more than one CIK — Entergy and Southern Company utility families filing
  one document for several registrants — giving 29 surplus artifacts, all with a
  single distinct SHA-256. Matching on `(cik, accession)` reconciles exactly, and
  that pair is the planner's filing identity.

The shared-CIK limitation was quantified rather than fixed: 264 CIKs are claimed
by more than one tracked identity, covering 620 identities, 69 of which keep
`cik=NULL`, across 20,428 month rows and 19,201 `UNKNOWN`. Almost all are
preferred and baby-bond tickers (`RILYG`, `AFGB`, `SOJC`, `MGRB`) — non-common
securities a common-stock universe excludes anyway — with `AIZ`/`AIZN` at CIK
1267238 the worked case. **Recommendation for a later phase: model CIK as
issuer-level evidence in its own relation rather than as a unique column on
`historical_securities`.** No migration was run.

Catalogue completeness was checked offline: of 2,604 referenced history files,
2,415 end before 2022 and only **189 could overlap the window**, so the 269,538
candidate set is essentially complete for the period being reconstructed.

Plan artifacts live under `derived/` — `expansion_plan_v1.{csv,jsonl,sha256}` and
`validation_batch_1.jsonl` — and are excluded from reconciliation: trusted ==
files_on_disk == 38,355, 0 orphans, and the corpus fingerprint is unchanged.

### Phase 9L.1 — what the corpus can actually support

The corpus was read offline and measured against the monthly universe. **Six
filings per security is not enough, and the reason is time, not volume.**

Extraction succeeded completely: 32,584 of 32,584 filings parsed, no failures and
no missing files, yielding 43,163 registered-class rows in 1,086 s. 30,093 of
those rows are common stock, 12,274 something else, and 796 the evidence cannot
place. Classification is overwhelmingly title-led (39,760 `TITLE_ONLY`, 2,532
`TITLE_AND_MEMBER`, 147 `CONFLICTING`), exactly as the 9K.2 policy intends.

The binding constraint is *when* the filings are from. A harvest capped at the
six most recent filings per security produced a corpus that is 98.2% dated 2022
or later and 81.9% dated 2026, spanning a median of **100 days** per filer. The
research universe spans 55 months. Six recent filings cannot describe it, and the
measured result is that this corpus closes only **8.0%** of the universe's
258,463 `UNKNOWN` month-verdicts at the current 90-day carry-forward.

Three rules were established here.

- **A source database is named, never inherited.** `bind_source` takes an
  explicit path, opens it `mode=ro`, and refuses unless the universe it holds
  matches the expected count and digest. This is the 9K.25 near-miss made
  impossible: the wrong database produced a fully green run over a population
  3,150 identities smaller, and nothing in the output said so.
- **A filing is a point, not an interval.** Everything that turns dated cover
  facts into monthly coverage is inference, so direct and carried figures are
  never pooled. Carry-forward is forward-only; measured across 0/30/60/90/120/180
  days it buys 0 / 61.5 / 107.1 / 147.2 / 186.0 / 263.3 percent more
  security-months over the exact-date floor. **90 days is retained** — it is the
  point where the curve is still steep without a filing speaking for two quarters
  it cannot see.
- **Derived output is not evidence.** The 229 MB index lives under `derived/`,
  excluded from reconciliation exactly as `checkpoints/` is, and is rebuildable
  from the manifest and the stored bytes. The corpus fingerprint was identical
  before and after the whole analysis.

What expansion would buy is now measured rather than guessed. Every qualifying
filing inside the universe's own window is 292,866 documents, of which 269,521
are not held; at measured per-form sizes that is **≈150 GiB** (8-Ks dominate the
count at 42 KB mean, while 10-Qs and 10-Ks carry the bytes). Fetching them would
raise the reachable `UNKNOWN` share from 8.0% to **76.7%**.

It cannot reach 100%, and the residue is a different problem: **37,949 monthly
rows (14.1%) belong to identities with no resolvable CIK**, and 978 of the 1,252
historical securities in the universe have no CIK at all. No quantity of SEC
filings answers those — that gap is a historical ticker→CIK limitation of the
free record, not a harvesting shortfall.

Two structural limits were also recorded. `historical_securities` carries
`UniqueConstraint("cik")`, so a multi-class filer cannot store both classes under
one CIK; the second keeps `cik=None` and loses its filer handle. And 2,053 of
5,771 submissions catalogues reference 2,604 further history files that were
never fetched, so even the *metadata* of the deep past is incomplete.

Retaining whole documents is justified rather than assumed: on a deterministic
400-document sample, 98.5% carry at least one material-event signal — 56.2%
earnings, 53.8% an explicit 8-K item, 49.5% acquisition language, 12.0% listing
or delisting events.

### Phase 9K.25 — the production SEC evidence corpus

> **Historical (Generation V1).** The corpus described below, its manifest
> and its checkpoints lived on the BEK drive, which is permanently
> unavailable as of B2.9X. The figures are a record of what was built, not of
> what exists. See [B2.9X](storage-layout.md#b29x-the-second-generation).

The corpus was complete. All 8,919 production identities had been harvested
across 36 chunks of 250 (the last holds 169), and the terminal state is **38,355
trusted artifacts** — 5,771 submissions catalogues and 32,584 filing documents —
over 38,540 manifest records and 37,695,067,178 logical bytes. Every artifact's
SHA-256 was recomputed from disk at the end and matched what the manifest
recorded.

29,479 HTTP requests were issued in this phase for 3 non-200 responses, all
`503 Service Unavailable` and all recovered by the retry policy: each of the
three URLs ends `DOWNLOADED status=200` in the manifest with real bytes and a
digest. No target finished `FAILED` or `SKIPPED`. Every one of the 28 chunks
converged on its third pass with zero new bytes, so two fetching passes were
always sufficient.

Two rules were added here.

- **Verification is tiered, and the tiers are reproducible.** Rehashing the whole
  corpus after every chunk reached 1,523 s at 8,963 artifacts and grows with the
  corpus, so 28 more of those would have cost more hours than the fetching did.
  Instead every chunk gets a full structural reconciliation and a full SHA of
  everything it wrote, plus a sample of the older corpus taken by a fixed rule —
  **sorted by `(sha256, url)`, even stride, `max(500, 5%)`**. The ordering comes
  from the artifacts' own digests, never from process state or the clock, so
  anyone can reproduce exactly which artifacts were checked. Five full-corpus
  rehashes at offsets 3000, 4250, 5500, 6750 and 8000 and a sixth at the end
  confirmed the tiers: no artifact the sampling passed was later found to have
  drifted.
- **The sweep's database is named explicitly, and read-only.** `.env` points
  `DATABASE_URL` at `compounder_radar.db`, but the production universe lives in
  `stage9k.db`; a sweep run without the override selects a *different* 5,769
  identities and every chunk still passes its gates. The completed checkpoints
  are what settle it — each stores the SHA-256 of its frozen identity list, and
  all six from 9K.23–9K.24 reproduce from `stage9k.db` and from nothing else.
  The sweep now opens it `mode=ro&uri=true` so it cannot be written at all.

Of the 6,919 identities in this phase, 4,763 (68.8%) resolved to a CIK and were
requestable — 4,475 through the EDGAR ticker map and 288 already stored — and
2,156 did not. An unresolved identity is recorded as such and never guessed at.

The primary database is untouched: `compounder_radar.db` is byte-identical
(`02a6660b…`, 249,155,584 bytes) with an unchanged mtime and no WAL or journal
sidecar, as is `stage9k.db` (`20a77d24…`), whose WAL is empty and predates the
sweep.

### Phase 9K.13 — the SEC evidence writer

Phase 9K.12 found that the SEC harvest had no code: `SEC_EVIDENCE_DIR` was
referenced only by the setting that declares it and the resolver that verifies
it. `src/stock_screener/sec_evidence/` is that subsystem — storage, manifest,
harvest and sample — plus `api_clients.SecDocuments`, which fetches one document
and returns the response facts a stored artifact needs to be auditable.

Four rules govern it.

- **The root is verified, never created.** `open_evidence_root` requires the
  configured directory to already exist and only then creates the four known
  subdirectories beneath it. An unmounted drive raises.
- **Writes are anchored to that root.** An earlier draft created missing parents
  by walking up to the nearest directory that exists, which protects nothing:
  `/Volumes` always exists, so an unmounted `BEK` would have had the whole
  evidence tree built underneath it on the boot disk. Directories are now only
  created *below* a root just confirmed present, and a destination outside it is
  refused.
- **An artifact is whole or absent.** Temporary file in the destination
  directory, flush, `fsync`, atomic rename. A partial download never becomes a
  file the next run trusts.
- **Cache reuse is proved, not assumed.** An artifact is reused only when the
  manifest records it as stored *and* the bytes still hash to the recorded
  digest. Anything else is `INVALID_CACHE` and re-fetched, and a file with no
  manifest entry is re-fetched too — adopting it would invent provenance.

Outcomes are `DOWNLOADED`, `CACHE_HIT`, `INVALID_CACHE`, `FAILED`, `SKIPPED`.
Budgets (`max_requests`, `max_bytes`) are checked before each request and end a
run with `SKIPPED` entries, which is what makes the next run a continuation. The
CLI is `stock-screener evidence status | sample | harvest`, and a dry run issues
no request and writes nothing, the manifest included.

**No harvest was run.** No network request was made in this phase.

### Phase 9K.11 — point-in-time occupant attribution

Attributing a monthly observation asked "is this symbol in `companies`?" first and
stopped there if it was, which reads today's roster as a description of every
month the symbol ever had. `BBBY` was the measured case: all 29 observed months —
Bed Bath & Beyond's 17 on Nasdaq through 2023-05 and Beyond, Inc.'s 12 on NYSE
from 2025-08 — were credited to the live company. One occupant where the record
plainly holds two, and survivorship bias written directly into the universe.

Three rules now govern it.

- **The first question is whether the symbol changed hands, not who holds it.**
  `classify_symbol` answers it from the published fails-to-deliver record. Where
  one issuer ever held a symbol, the live row and any stored delisted row are the
  same business and the live one answers, exactly as before.
- **`last_trading_date` is not the boundary, though it reads like one.** Of the
  138 symbols carrying both a live row and a delisted row, **127** have a last
  trading date within days of the present, because the free record's last trading
  date is the last settlement its files happened to show. Leggett & Platt is not
  its own dead predecessor. Trusting that field would have moved 127 symbols off
  the live roster wholesale.
- **The live roster is reachable only from the most recent span.** Inside a
  sequence, `holder_spans` names the month's occupant and `security_identifiers`
  says which stored security carried that issuer's CUSIP. A month between two
  occupants' evidence is `REUSED` — recorded as a change of hands whose holder
  cannot be named — never handed to whoever holds the letters now.

The same defect had a mirror image in the class join: `build_universe` passed one
occupancy span, and `occupancy_of` places a date before the first run with the
first occupant, so a predecessor's cover page could still classify a successor's
months. A live company that inherited its symbol now carries its predecessor's
tenure as a run of its own, and the occupancy test refuses the old filing.

Measured over the staging record (458,708 observations, 268,780 monthly verdicts):

| | before | after |
| --- | --- | --- |
| `RESOLVED_COMPANY` | 233,480 | 226,469 |
| `RESOLVED_HISTORICAL` | 33,458 | 45,492 |
| `REUSED` | 11,629 | 35,102 |
| `UNRESOLVED` | 180,136 | 151,530 |
| `ELIGIBLE` monthly verdicts | 9,256 | 9,066 |
| live securities ever `ELIGIBLE` | 1,714 | 1,659 |
| delisted securities ever `ELIGIBLE` | 486 | 522 |

301 `ELIGIBLE` rows moved off a live company. 55 live companies lost months they
never traded in and 36 delisted securities gained months they did.

## Architecture

### Two deployables, one repository

A deliberate exception to the one-deployable rule. They live together because the
dashboard's types mirror these endpoints and a response-shape change must land on
both sides in the same commit. **Do not move the frontend to another repository.**

| Deployable | Is | Owns |
| --- | --- | --- |
| `src/stock_screener/` | Python backend, CLI, read-only API | all data, every calculation, every score |
| `frontend/` | the dashboard, Next.js | rendering what the API returns |

A third deployable needs an ADR first and probably its own repository.

### Packages

| Package | Import | Holds | I/O |
| --- | --- | --- | --- |
| `packages/domain` | `domain` | models, metric engine, eligibility, CompounderScore, curves, sectors, statements, cadence, survivorship | none |
| `packages/data-access` | `data_access` | SQLAlchemy models, repositories, session | database |
| `packages/api-clients` | `api_clients` | Alpaca, EDGAR, FMP, research providers, external search, FX | HTTP |
| `packages/research` | `research` | Phase 3 contract, prompt, validation | none |
| `packages/deep-research` | `deep_research` | Phase 6 contract, brief, evidence, provenance, prompt, validation | none |

Placement rule: one caller → a module in `src/stock_screener/`; two or more
callers → a package. Never create one speculatively.

### Application modules

```
src/stock_screener/
  cli.py            every command; the only entry point a job may spawn
  api.py            the read-only API plus the job endpoints
  config.py         the typed settings — the only reader of the environment
  dashboard.py      read models for the dashboard endpoints
  providers.py      provider wiring and composition
  fx.py             rate resolution and storage
  jobs.py           JOB_KINDS, spawn, track
  logging.py        structlog setup
  scanning/         ingestion, scanner, report
  scoring/          engine, enrichment, rankings, report
  research/         candidates, brief, runner, report, store
  deep_research/    preparation, collection, sources, brief, runner, report
  pit/              point-in-time capture, metrics, actions, returns, delistings
  signals/          market-signal derivation from the feature record
  backtest/         readiness harness, score reconstruction, historical securities
```

### Frontend routes

`/` · `/stocks/[ticker]` · `/watchlist` · `/jobs` · `/research` ·
`/research/[ticker]`

---

## Score definitions

### Versions

| Version | State | What it is |
| --- | --- | --- |
| `COMPOUNDER_V1` | history only | the original rules |
| `COMPOUNDER_V1_1` | history only | V1 plus three guards; no weight or curve changed |
| `COMPOUNDER_V1_2` | **current** | V1.1's formulas exactly, plus one ranking-admission policy |

`CURRENT_SCORE_VERSION = COMPOUNDER_V1_2`, in
`packages/domain/src/domain/scores.py`.

**V1.1's guards:** redistribution capped at a 1.15 uplift; acceleration credit
withheld from a rebound against a flat three-year trend; revenue growth plus
acceleration capped where growth does not persist.

**V1.2's policy:** a score built on `STALE` fundamentals is excluded from current
rankings. Every formula, curve, weight, threshold and penalty is V1.1's. It
needed a version because it changes what a ranking *is*, not because a number
moved.

### Components

| Component | Points |
| --- | --- |
| Growth | 35 |
| Financial Quality | 25 |
| Valuation | 25 |
| Market Confirmation | 15 |

Risk penalties are applied after; full rules in
[CompounderScore](compounder-score.md).

### Scoring status

`SCORED` · `INSUFFICIENT_DATA` · `UNSUPPORTED_SECTOR` · `NOT_ELIGIBLE` · `ERROR`.
`final_score` is a number only for `SCORED`.

### Freshness

`CURRENT` · `STALE`. Recorded on the snapshot, never derived by the reader,
because the bound scales with reporting cadence. `domain.is_rank_eligible` is the
single place the question is answered. Rows written before migration `0020` read
as `CURRENT`.

| | Stale score |
| --- | --- |
| Numerical score | kept |
| Stock detail page | shown, with a notice |
| Research and Deep Research | available |
| Historical snapshots | untouched |
| Current rankings — all four views | excluded |

### Ranking views

`rankings` (top) · `hidden-gems` · `wrong-price` · `improving`. All four are
current views and all four exclude stale scores.

---

## Data contracts

### Domain enums

| Enum | Members |
| --- | --- |
| `ScoringStatus` | `SCORED`, `INSUFFICIENT_DATA`, `UNSUPPORTED_SECTOR`, `NOT_ELIGIBLE`, `ERROR` |
| `Freshness` | `CURRENT`, `STALE` |
| `ExclusionReason` | `PRICE_BELOW_MINIMUM`, `MARKET_CAP_BELOW_MINIMUM`, `LOW_LIQUIDITY`, `INACTIVE`, `UNSUPPORTED_SECURITY_TYPE`, `UNSUPPORTED_CURRENCY`, `MISSING_REQUIRED_DATA` |
| `PeriodCadence` | `QUARTERLY`, `SEMIANNUAL`, `ANNUAL`, `UNKNOWN` |
| `StatementProfile` | `GENERAL`, `FINANCIAL_INSTITUTION` |
| `MarketCapSource` | provenance of a market cap; `CALCULATED` is withheld from 20-F/40-F filers |
| `VolumeBasis` | which tape a liquidity figure came from |

Anything a package exports lives in its `__all__`. Reaching into a submodule from
outside is a bug.

### Eligibility thresholds

`min_price` 2.0 · `min_market_cap` 100,000,000 · `min_avg_dollar_volume`
1,000,000 · `min_trading_days` 20. Defaults mirror `.env.example`; the
application builds `EligibilityThresholds` from `Settings`.

### Prompt versions

`research.CURRENT_PROMPT_VERSION = RESEARCH_PROMPT_V2` ·
`deep_research.CURRENT_DEEP_PROMPT_VERSION = DEEP_RESEARCH_PROMPT_V2`. A prompt
version change invalidates the deep-research cache.

### API surface

Read-only unless marked.

```
GET    /health
GET    /api/companies
GET    /api/companies/search
GET    /api/companies/{ticker}/score
GET    /api/scan
GET    /api/rankings
GET    /api/rankings/hidden-gems
GET    /api/rankings/wrong-price
GET    /api/rankings/improving
GET    /api/rankings/{view}/export
GET    /api/stocks/{ticker}
GET    /api/stocks/{ticker}/research
GET    /api/deep-research/{ticker}
GET    /api/deep-research/{ticker}/history
GET    /api/deep-research/reports/{report_id}
GET    /api/watchlist
POST   /api/watchlist/{ticker}          write
DELETE /api/watchlist/{ticker}          write
GET    /api/jobs/kinds
POST   /api/jobs                        write, spawns a process
GET    /api/jobs
GET    /api/jobs/{job_id}
```

### Job kinds

`daily` · `scan` · `score` · `enrich` (spends money) · plus the research kinds.
The map in `src/stock_screener/jobs.py` is the entire allowlist.

### CLI

`update-universe` · `update-market` · `update-eligibility-volume` ·
`update-benchmark` · `update-fundamentals` · `scan` · `run-scan` · `score` ·
`enrich` · `rankings` · `hidden-gems` · `wrong-price` · `improving` · `explain` ·
`run-daily`, plus the `research` group (`candidates`, `brief`, `update-filings`,
`update-filing-text`, `run`), the `deep-research` group (`prepare`, `run`)
and the `evidence` group (`status`, `sample`, `harvest`, `harvest-manifest`,
`sweep`, `migration-plan`, `generation-plan`, `b2-canary --confirm`,
`rebuild-canary --live`, `rebuild-baseline --live`).

---

## Database migrations

Head is **`0030`**. One revision per schema change; the chain is linear.

| Rev | Adds |
| --- | --- |
| `0001` | initial schema — `companies`, `financial_snapshots`, `price_history` |
| `0002` | `reporting_currency` |
| `0003` | consolidated average daily share volume |
| `0004` | `score_snapshots` — one score per company per day per version |
| `0005` | `benchmark_prices` |
| `0006` | point-in-time share counts and market-cap provenance |
| `0007` | gross-profit basis — which concept produced the figure |
| `0008` | `research_reports` |
| `0009` | `filings` |
| `0010` | `filing_excerpts` |
| `0011` | `watchlist` |
| `0012` | `jobs` |
| `0013` | `deep_research_reports` |
| `0014` | external collection reuse |
| `0015` | score coverage — market run or single company |
| `0016` | `score_snapshots.exclusion_reasons` |
| `0017` | `fx_rates`; splits reporting currency from quote currency |
| `0018` | `period_start` and `cadence`; existing rows backfilled `QUARTERLY` |
| `0019` | `statement_profile`, `consolidated_avg_volume`, `volume_source` |
| `0020` | score freshness; NULL reads as `CURRENT` |
| `0028` | `universe_membership` — effective-dated membership verdicts against both identity kinds, with the confidence behind each. Additive; no existing table touched |
| `0029` | `pit_feature_snapshots` and `pit_signal_snapshots` gain dual identity |
| `0030` | `listing_source_files`, `listing_observations` — free monthly venue evidence |
| `0027` | `security_identifiers`, `ftd_source_files`, `ftd_identity_observations` — the identity layer. Relaxes `historical_securities.cik` to nullable and opens `forward_returns` to a second identity with a check constraint and two partial unique indexes. Rebuilds two tables; see the safety note below |
| `0026` | `historical_securities` and its `historical_observations`, `historical_prices`, `historical_corporate_actions` children — delisted names keyed by CIK, separate from `companies`. Additive, no `batch_alter_table` |
| `0025` | `delistings` — the securities that left the market, keyed by ticker because they have no company row |
| `0024` | `forward_returns` — what a security did at 1/5/20/60/120 sessions from an observation date |
| `0023` | `filing_acceptance` — when the SEC accepted each filing, so availability is judged by instant rather than by date |
| `0022` | `pit_signal_snapshots` — the derived market-state layer, append-only and versioned |
| `0021` | the point-in-time layer — `pit_capture_runs`, `pit_score_snapshots`, `pit_feature_snapshots`, `fundamental_observations`, `universe_events`, `corporate_actions` |

### Tables

`companies` · `financial_snapshots` · `price_history` · `benchmark_prices` ·
`fx_rates` · `score_snapshots` · `filings` · `filing_excerpts` ·
`research_reports` · `deep_research_reports` · `watchlist` · `jobs`

Phase 8B's append-only layer, beside them: `pit_capture_runs` ·
`pit_score_snapshots` · `pit_feature_snapshots` · `fundamental_observations` ·
`universe_events` · `corporate_actions`

Phase 8C adds one more: `pit_signal_snapshots`.

Phase 8I adds the historical-security layer, separate from every table above:
`historical_securities` · `historical_observations` · `historical_prices` ·
`historical_corporate_actions`.

Phase 9A adds the identity layer over it: `security_identifiers` (dated CUSIP,
ticker and venue intervals against a stable `security_id`) and the evidence they
are built from — `ftd_identity_observations` and `ftd_source_files`.

### The migration safety rule

**Never verify a migration against `compounder_radar.db`.** Use a temporary
database or a throwaway copy.

A migration is not only DDL: `batch_alter_table` rebuilds a table on SQLite, and
dropping `companies` under enforced foreign keys cascades through every child
declaring `ON DELETE CASCADE` — price history, fundamentals, scores, filings,
excerpts, both research tables and the watchlist. Migration `0017` did exactly
that and deleted about 1.6 million rows from the primary database. That is why
`tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows`
exists.

**`0027` rebuilds two tables, and one of them is a parent.** Making
`historical_securities.cik` nullable needs a SQLite table rebuild, and that table
has three children declaring `ON DELETE CASCADE`. The revision disables foreign-key
enforcement for the rebuild — which SQLite's own documentation prescribes — and the
subtlety that makes it dangerous is that `PRAGMA foreign_keys` is **silently
ignored inside a transaction**, which Alembic normally has open. The naive version
appears to work, does nothing, and cascades. `_disable_foreign_keys` commits at the
driver level first and then *verifies* the pragma took effect, raising rather than
proceeding if it did not: a migration that cannot make itself safe must fail loudly.
`tests/integration/test_migrations.py::test_the_identity_migration_preserves_a_populated_historical_security`
is the proof on the real upgrade path. The `forward_returns` rebuild needs no such
care — nothing has a foreign key *to* it, so dropping it cascades to nothing.

---

## Known limitations

**The external paper endpoint is implemented but not certified (P2.2C).** The
concrete HTTPS transport exists and is verified offline against
`httpx.MockTransport`, but nothing in this repository has spoken to a real
venue. Two independent blockers: no `ALPACA_PAPER_*` credential is configured,
and the `read_fills` defect below would break exactly-once fill ingestion on a
real account.

**A fake transport cannot establish provider correspondence (P2.2D).** All four
defects P2.2D closed had passed 211 fake-transport tests, because a fake answers
whatever it is told to. Offline tests confirm a protocol has the shape the code
expects; only the documented surface, or a real call, shows whether that shape
matches the provider. This is a standing limitation of the test strategy, not a
bug that was fixed.

**Operational control is not execution quality (P2.3).** The control plane can
establish that a run was permitted, reconciled and recorded. It says nothing
about whether the orders it permitted were good ones. Gating is not judgement.

**There is no strategy authority (P2.3.1).** The authority chain an
operational run needs is
inputs → strategy → portfolio construction → risk → execution plan → broker →
accounting → reconciliation. Every link from portfolio construction onward
exists and is production-capable: `portfolio_risk.constructor.construct`,
`PORTFOLIO_RISK_ENGINE_V1_2`, `execution.planner` under
`EXECUTION_ENGINE_V1_2`, the certified `AlpacaPaperAdapter`,
`PORTFOLIO_ACCOUNTING_V2`, and the three-way reconciliation. The first link does
not exist. `StrategyIntent` is constructed in exactly one production location —
`portfolio_risk.run.load_request`, reading a JSON request — so the system
authorises, sizes, routes, fills and books a decision it has no way to *make*.
That is a deliberate boundary rather than an oversight: the screener ranks
companies and the research layer explains them, and neither was ever given
authority to size a position. Closing it is a strategy-definition phase, not
integration work, and inventing a toy strategy to unblock the soak would make
the soak evidence about the toy.

**H2 cannot be borrowed as that strategy (P2.3.1).** It is the only frozen
decision rule in the repository, and its own `PROMOTION_LADDER` records
`BROKER_PAPER` as `NOT AUTHORISED — requires its own approved phase`, with
`paper_orders: NOT AUTHORISED` in the protocol beside it. Using it for paper
operations would contradict the record that makes it evidence.

**The soak is armed and has produced no evidence yet (P2.4).** The protocol is
frozen and the recorder works, but a preregistration is a promise about how
future evidence will be judged, not evidence. Everything known about this
control plane under repetition is still inference from tests. That is the
condition P2.4 exists to end, and it ends by waiting.

**The drills are injected, not real (P2.4).** They establish the decision the
control plane makes when a provider is unreachable or a submission's fate is
unknown; they establish nothing about the provider's behaviour during a genuine
outage. The P2.2E canary remains the only evidence in this programme that came
from a real venue, and it found two defects that 211 fake-transport tests had
passed. A fake transport proves a protocol's shape and nothing about its
correspondence to reality.

**Twenty sessions is a floor chosen before the data, not a sufficiency proof
(P2.4).** It is enough to exercise every drill and to see a month of calendar
variation. It is not enough to bound the rate of a
failure mode that occurs once a quarter. A clean soak means the control plane
operated as specified over the preregistered window; it is not a live trading
authorisation, and this package has no path to one.

**The soak calendar is a declaration, not an observation (P2.4).**
`SOAK_SESSIONS` is weekdays minus four named closures and two early closes,
written down before the first of them, because a forward calendar cannot be read
from stored bars. If
the exchange closes unexpectedly, a listed session will not happen and the soak
will show it as an eligible session with no record — which blocks promotion
until a person says why, and that is the correct outcome.

**Coordinated local rollback remains undetectable (P2.3, P2.4).** The run
journal inherits the limit the execution journal and accounting ledger already
carry: a rollback of both the journal and its anchor to a mutually consistent
earlier state cannot be detected locally. The soak's observation files inherit
it too. Four records now share one unclosed gap, and an independently trusted
external anchor would close all four. Nothing on a single filesystem can
establish this about itself.

**Certification is one observation, not a guarantee (P2.2E).** It establishes
that this code, against this provider, at one moment, did what it claims. An
external run is not reproducible — the fills came from a matching engine at a
moment that will not recur — and the artefact distinguishes reproducible
code/config semantics from recorded-but-unrecreatable external observations.

**A provider's paper engine is the provider's model of itself (P2.2E).** It may
fill more readily than its live engine would, it is not bound by this project's
participation ceiling, and its queue behaviour is unobservable from here. A
paper fill is evidence the plumbing works, not evidence the trade was
achievable. Certification says nothing about execution quality.

**A canary needs an account this book fully accounts for (P2.2E).** Any
pre-existing holding reconciles as `REMOTE_POSITION_MISMATCH` (BLOCKING) and any
foreign working order as `UNEXPECTED_REMOTE_ORDER`. The preflight reports
`canary_ready` and names the blockers; clearing them is the account owner's
decision, and nothing here does it automatically.

**A canary run is not repeatable without a new run tag (P2.2E).** The client
order id is derived from the plan id, so a fixed plan id makes every run derive
the same venue-facing id — which the venue refuses as a duplicate and accounting
refuses as a past event. That is correct behaviour, and it means the plan id
must be run-scoped. Randomising it would trade away the crash-idempotency the
derivation exists for.

**Making unrelated external state non-blocking would be a semantics change.**
Reconciliation currently treats any unexplained remote holding as BLOCKING,
which is correct: it cannot tell a holding somebody else opened from a fill this
book failed to record. Distinguishing them needs a declared
acknowledged-external baseline, which is a deliberate change to P2.2's
reconciliation contract and is out of scope for a repair phase.

**An external run is not reproducible (P2.2).** Its fills came from a venue's
matching engine at a moment that will not recur. The V1.2 manifest is an audit
record of what was bound and what happened, not a claim that re-running it
reproduces it, and no test can assert an external fill's price. A V1.2 run bound
to `LOCAL_DETERMINISTIC_PAPER` reproduces normally.

**A provider's paper engine is the provider's model of itself (P2.2).** It may
fill more readily than its live engine would, it is not bound by this project's
participation ceiling, and its queue behaviour is unobservable from here. A paper
fill is evidence that the plumbing works, not evidence that the trade was
achievable.

**Proof of paper from configuration is not proof of reach (P2.2).** A host
allow-list and a key prefix establish what was configured. Only the account's own
answer adds more, and only for as long as the credential is unchanged — a
credential repointed after preflight is not detected until the next read.

**An unresolved submission needs a person (P2.2).** The protocol establishes that
a submission's state is unknown and refuses to guess; settling it is a deliberate
decision. Nothing auto-resolves it, and preflight withholds readiness until it is
settled, so an unresolved intent blocks the next run by design.

**The execution friction is declared, not measured (P2.1).** Slippage,
participation and the session horizon are policy inputs. Nothing calibrates them,
and nothing should calibrate them from a strategy's own returns — doing so would
make the cost model a free parameter of the strategy and its apparent edge partly
an artefact of the assumption chosen. Every example value is
`NOT_PRODUCTION_AUTHORIZED`, and a realistic figure needs an execution-cost study
this programme has not performed.

**No market impact, latency or queue position (P2.1).** V1.1 models quoted
spread, declared adverse slippage and share-volume participation. It does not
model impact, which is a function of an order's own size against available
liquidity — a flat haircut is not an approximation of it, and a large order will
be simulated as cheaper than it would trade. Latency, queue position, hidden
liquidity, venue routing and auction behaviour are likewise absent.

**One intraday execution opportunity per session (P2.1).** A session snapshot is
a single opportunity at its `as_of`, so an order cannot work the session's price
path. That is what keeps the timing semantics honest — using a whole session's
volume while dating a fill earlier inside it would be leakage — but it also means
intra-session timing cannot be studied here.

**A session with no snapshot stops that security's rollover (P2.1).** No child is
created for a session the request supplied no market for, because a child with
nothing to fill against would expire having never had a chance. The outcome
reports `NO_MARKET_SNAPSHOT_FOR_SESSION` rather than skipping to a later session.

**V1 execution carries the limitations P2.1 closed.** `EXECUTION_ENGINE_V1`
remains executable and reproducible, and a V1 run still models no execution cost
at all and still lets a DAY order live indefinitely. Those are properties of that
frozen version, not open defects: the fix is to run new work under V1.1.

**Coordinated local rollback of an execution journal and its anchor is not
detectable (P2.0).** The same limitation as the accounting ledger's, for the same
reason: local files cannot establish that they are the latest state of
themselves. An independently trusted external anchor would close both.

**One plan per execution journal generation (P2.0).** Running a second plan
against the same book needs a new execution store, and there is no cross-plan
order view. Adequate for a single rebalance; a continuously trading system would
need a journal that spans plans.

**Journal events must arrive in non-decreasing occurrence order (P2.0).** The
journal is ordered by occurrence, so a caller sequencing a sell's fill after a
later buy's submission is refused. That matches the sequence a no-margin book
actually requires — sells must fill before buys are funded — so it is a
constraint on callers rather than an internal one, but it is a real constraint.

**Coordinated local rollback of a ledger and its anchor is not detectable
(P1.4).** A ledger and its local head anchor rolled back *together* to a mutually
consistent earlier state validate cleanly. Anyone able to delete ledger records
is generally able to rewrite the file beside them, and local files cannot
establish that they are the latest state of themselves — this is a property of
where they live, not an implementation gap. Closing it needs an anchor held where
the ledger's writer cannot reach; `ExternalAnchorSink` is the seam and P1.4
implements no remote sink. There is a test asserting this limitation honestly
rather than one claiming otherwise.

**Per-strategy dividend income is not claimed (P1.4).** The event contract states
an aggregate cash entitlement, so a dividend is credited to the unknown-owner
pool rather than divided across today's holders — who are not necessarily the
owners who earned it, since a position sold between the ex-date and the pay date
still earns it. Global dividend income is exact; per-strategy income is not
asserted. The event contract was deliberately not widened for this.

**An imported position with several owners and one stated basis seeds every pool
at that basis (P1.4).** An opening balance states a lot's cost per share, not the
owners' separate entry prices, so each named owner's pool inherits the stated
figure. That is faithful to what the event asserts; the separate prices are not
in it and are not invented.

**No tax accounting (P1.4).** `PER_ATTRIBUTION_WEIGHTED_AVERAGE_COST_V1` is an
internal accounting basis, not a tax basis: no FIFO, LIFO or specific-lot
identification, no holding periods, no wash-sale rules and no tax character. A
test asserts no class or function in the package implements lot selection.

**No accrued interest, margin or multi-currency in the books (P1.3, still open).**
Cash is a single balance in one implied currency, and the long-only baseline has
no borrowing.

**V1 accounting carries the limitations P1.4 closed.** `PORTFOLIO_ACCOUNTING_V1`
remains executable and reproducible, and a V1 ledger still pools cost across
strategies (so its per-strategy realised P&L is exact only when co-owners entered
at the same price), still loses the owner of a fully closed position's realised
P&L, and still needs a checkpoint for truncation detection. These are properties
of that frozen version, not open defects: the fix is to open new books under V2,
or to migrate by replaying the events.

**The legacy colocated layout is still supported, and is one failure domain.**
Every corpus harvested before B2.5 — the BEK corpus included — keeps its
manifest, checkpoints and derived index beneath `SEC_EVIDENCE_DIR`, and with
`SEC_CONTROL_DIR` unset the code reads that layout unchanged. No control file
was moved; relocating an existing corpus's control files is a deliberate
operator step for a later phase, as is any remote mirror of them. `EvidenceRoot`
and the `harvest()`/`preflight()` compatibility forms remain for that layout.
Backend temporaries are hidden from artifact listing (`temporary_files` remains
zero). The local backend does not pin mount identity against concurrent
replacement. See [`EvidenceRoot` after B2.5](storage-layout.md#evidenceroot-after-b25).

**The bucket's daily caps bound a run, and the terminal walk is one pass.**
A baseline chunk downloads its own targets twice (read-back and replay), the
terminal gate downloads the whole corpus once, and `reconcile` costs three
HEADs per artifact; Backblaze's default Caps & Alerts refused the chunk 000
replay after 2,258 GETs, and the same run completed once the caps were
removed — keep them removed. The terminal walk has no state to resume from:
a fault inside it, after its reads' own retries, means the whole pass again
(5 h 22 min over 38,146 artifacts). See
[B2.11](storage-layout.md#b211-the-baseline-rebuild).

**Historical listing evidence is monthly and partly censored.** Free sources give
Nasdaq and NYSE from 2022-01 at month resolution only; NYSE American is not
covered at any date, and no free daily listing source exists. A 2022+ universe
built on it is survivorship-aware at monthly resolution and censored below that.
See `docs/reference/listing-evidence.md`.


Documented rather than fixed. Each is a deliberate stop, not a backlog item.

| Limitation | Consequence |
| --- | --- |
| **6-K interim statements** — an untyped envelope; most carry no financial statements and the ones that do are indistinguishable without opening them (hundreds of MB per filer) | Foreign quarterly reporters that publish interims only via 6-K become `STALE` and leave current rankings. All 191 current-rank-eligible foreign companies are annual filers. 6-K exhibit discovery is not to be implemented without being asked. |
| **us-gaap capital expenditure basis** (`KNOWN_CAPEX_BASIS_LIMITATION`) | 7 affected names, none in the Top 100, maximum score sensitivity 0.04 points. Normalisation unchanged. |
| **Filing-instance bandwidth** | 458.7 MB of 613.2 MB total traffic; median instance 5.45 MB, maximum 49.6 MB. Where it fails it fails safely — CGAU kept its 2023 history. |
| **Annual-only period semantics** | TSM, ASML, SAP and NVO remain unrankable. Currency is no longer their blocker; period semantics is. They get a correct currency, a stated exclusion reason and an indexed 20-F — not a score. |
| **No ADR ratio lookup** | Cover-page share counts on 20-F/40-F are not read; `market_cap_source` falls to `PROVIDER` or `UNKNOWN`. |
| **Single-exchange price bars** | The IEX share of consolidated volume ranged from 0.006% to 12% across 259 companies, so the $1M gate cannot be applied to stored bars. `update-eligibility-volume` reads the consolidated tape instead. |
| **No authentication on the job endpoints** | They spend money. Only reasonable because the API binds localhost. |
| **FDIC-only bank fundamentals** (historical layer) | First Republic and Signature file under §12(i) with the FDIC, so EDGAR `companyfacts` returns 404. Their prices are free via Alpaca; their fundamentals are recorded as `NO_EDGAR_FUNDAMENTALS` and left UNKNOWN, never guessed. |
| **Pre-2009 XBRL** (historical layer) | XBRL was not mandatory before 2009, so a filer that delisted earlier (Lehman) has no machine-readable fundamentals. Recorded, not reconstructed. |
| **Pre-~2021 free price history** (historical layer) | Alpaca's free IEX feed does not reach back past roughly 2021 (AAPL 2016, Sprint 2020, Sears 2018 all return no bars), so an older delisting is `NO_FREE_PRICE_HISTORY` and cannot be given a point-in-time market cap. |
| **Receivership terminal value** | No corporate-action feed files a receivership, so a failed bank's terminal equity value is UNKNOWN unless asserted from evidence outside the feed — never a guessed 0 or -100%. |
| **Fails-to-deliver silence** (identity layer) | A security whose deliveries never failed is absent from the SEC's record entirely, so its identity is unrecoverable from that source. Produces `UNRESOLVED`, and `EvidenceVerdict` has no `ABSENT` member, so no code path can turn it into a claim of non-existence. |
| **Pre-2008-09-16 threshold censoring** (identity layer) | Before that date the fails file carried only failures above a reporting minimum, so an absence over an earlier window is uninformative for a second, independent reason. Flagged as `threshold_censored`, not corrected. |
| **No corporate action in the fails record** (identity layer) | A CUSIP change within one issuer cannot be attributed to a reverse split, a re-incorporation or a class re-designation. Recorded as `CUSIP_CHANGE` at `INFERRED` confidence; `REVERSE_SPLIT` is only ever `ASSERTED` from evidence outside the feed. |
| **A re-incorporation is indistinguishable from a change of hands** (listing layer) | Because of the row above, PIT attribution reads KKR & Co. L.P. becoming KKR & Co. Inc. as two occupants, exactly as it reads Randgold becoming Barrick. Of the 75 symbols whose `ELIGIBLE` months moved in Phase 9K.11, 32 carry plainly different issuer names (`GOLD`: Randgold → Barrick → GOLD.com; `PARA`: Premara → Paramount → Banzai; `WELL`: Capsalus → Welltower) and 43 share a name stem and are likely re-incorporations or class changes (`KKR`, `CCL`). The layer takes the conservative side and reports `REUSED` for both, costing 301 `ELIGIBLE` rows — 3.3% of 9,256. Separating them needs a corporate-action source the free record does not carry. |
| **No venue on an FTD-derived interval** (identity layer) | The file carries no exchange, so `security_identifiers.venue` is NULL. "Not established", never a default. |
| ~~**No historical universe membership**~~ | **Addressed by Phase 9C**, which added a dual-identity `universe_membership` layer. 51 month-ends now reconstruct with a median of 4,953 members, 199 of them delisted. What remains is that no member verdict can be `OBSERVED` — see below. |
| **No `OBSERVED` membership is achievable** (membership layer) | Exchange, security type and sector are undated everywhere in this system, so every one of the 6,049 member intervals is `INFERRED` and names the attributes it carried back. Closing this needs a dated listing source — SEC Form 8-A/25 filings carry one — which is a separate ingestion, not a code change. |
| **A contaminated live row carries one continuous membership** (membership layer) | Where a `companies` row holds two issuers' history, its membership interval spans both: the layer separates the two *identities* correctly but cannot un-mix a row that is itself two companies. 1,486 live rows carry more than one CUSIP issuer under their ticker; 1,458 hold a single open `MEMBER` interval. Repairing this means splitting prices, filings and scores across two issuers, which is a separately-authorised data operation. |
| **Free price history begins ~2020-07** (outcome layer) | Alpaca's free IEX feed serves nothing earlier, so a security that delisted before then has an identity and a terminal event but no measurable outcome. 581 of 2,745 backfilled securities returned no bars at all. |
| **All-stock mergers state no consideration** (outcome layer) | PXD's exit into XOM records an exchange ratio and no cash, so its terminal value is `UNKNOWN`. Computing one would need the acquirer's price on the effective date, which is a second security's history and a deliberate later step. |

---

## Completed experiments

Measured runs, kept because they are the evidence behind decisions above.

### R1.7 prospective H2 shadow runner activated and armed — 2026-09-25

- `prospective activate` then `prospective arm`, each verifying the R1.6
  candidate `a7ace1d1…`, protocol `60c17136…`, prospective contract `bcade412…`
  and completion marker `ba028a9f…` by digest before changing any state.
- **Ordering used (R1.6 §78 two-stage):** implementation → targeted tests →
  `make check` → **one tracked-code commit** → post-commit gate → clean tree →
  activation bound to that exact commit → arming → control mirror → no tracked
  file changed afterwards, and the commit was not amended.
- **`NYSE_SESSION_CALENDAR_V1` validated against the record**: replayed over the
  frozen benchmark sessions it reproduces **1,531** sessions from 2020-07-27 to
  2026-08-28 with 0 additions and 0 drops. `bind_runner` refuses on any
  disagreement. Good Friday is computed, Juneteenth applies from 2022, and a
  Saturday New Year's Day is correctly not observed.
- **No order capability.** `order_capability_reachable` parses each module and
  inspects imported names, attribute accesses and call targets; it returns empty
  for the runner and detects a real `submit_order` / `TradingClient` path in a
  fixture. No `force` parameter exists; no broker credential is read.
- **Activation**: one immutable record, instant from the runtime clock, bound to
  a full commit. A supplied timestamp, a non-commit reference and a dirty tree
  are each refused. A restart re-reads the stored record rather than minting a
  new one — idempotency keys on the binding (commit, candidate, protocol,
  contract, semantic fingerprint), not on the clock. `observations_at_activation:
  0`.
- **Arming and derivation**: the first eligible decision is the first quarter-end
  strictly after activation for which the runner was armed by the close of the
  preceding session. Derived at arming, `derived_automatically: true`,
  `manually_supplied: false`, and recorded with its preceding session, required
  cutoff and comparison result. Arming exactly at the cutoff passes; one minute
  later skips the quarter to the next.
- **Semantic fingerprint** over 13 modules spanning the universe, features,
  score inputs, ranking, selection, target count, identity, execution reference
  and accounting. A moved fingerprint halts confirmatory capture as
  `SEMANTIC_RESEARCH_CHANGE`; a missing module is an error rather than a skip.
- **Packet discipline implemented and tested**: decision packets reject every
  outcome field at construction; execution packets link by digest and never
  modify a decision; corrections change neither and a material one yields
  `PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT`; `finalise` accepts an identical retry
  and raises `PacketConflictError` on different bytes under the same key.
- **Paired construction**: `paired_targets` takes one ranked cross-section and
  returns both target sets, so separate fetching is impossible.
  `ordinary_target_count(746) == 74` and `(1010) == 101`, matching R1.2 exactly;
  with no incumbents the two sets are equal, which is the initial-formation
  invariant.
- **Status is sealed**: criteria `SEALED_UNTIL_WINDOW_COMPLETE`, P1–P4 each
  `NOT_YET_EVALUATED`, verdict `PENDING`, performance
  `NOT_EVALUATED_FOR_RESEARCH_DECISION`, no turnover reading exposed, and the
  command fetches nothing to display.
- **Recovery proven**: after a simulated restart the runner recovers activation,
  arming, the derived first decision, the observation count, the transition count
  and protocol health from durable state alone, creating no duplicate records —
  the ledger shows exactly one `PROTOCOL_ACTIVATED` and one `RUNNER_ARMED`.
- **State separation**: runtime state lives under `.evidence/prospective/h2_v1/`
  and is mirrored to `control/PROSPECTIVE_SHADOW_STREAM_V1/`, never under
  `research/r1_*`. The frozen R1.6 record re-hashes unchanged after activation.
- **0 observations, 0 transitions, 0 decision packets, 0 broker connections, 0
  orders.** This is the successful end state.

### R1.6 H2 candidate frozen and prospective protocol defined — 2026-09-25

- `backtest freeze-h2-candidate --runs 2 --mirror` with `--expect-contract-sha`
  `f7efb001…` and `--expect-database-sha` `20a77d24…`. **8 binding checks**: the
  contract, the H2 record `56f2e51f…`, the R1.5 specification `6d266448…`, its
  evaluation `4d006e50…` and completion marker `9fb0f28b…`, plus R1.5's outcome
  being SUPPORTED, exploratory, and claiming no confirmation.
- **0 observations, 0 decision packets, 0 data requests, 0 broker connections,
  0 orders.** `capability_reachable` scans the package for `simulate`,
  `PriceBook`, `performance`, `httpx`, `requests`, `alpaca`, `broker`,
  `create_engine` and the rest, and returned empty.
- **Candidate `H2_MEMBERSHIP_STABILITY_CANDIDATE_V1`** carrying all four state
  words: `FROZEN_CANDIDATE`, `PROSPECTIVE_CONFIRMATION_PENDING`,
  `NOT_PRODUCTION`, `NO_LIVE_CAPITAL`. Bound to the H2 record, the R1.5
  specification, evaluation and marker, `BACKTEST_DATA_CONTRACT_V1`,
  `COMPOUNDER_V1_2`, `CLOSE_SIGNAL_NEXT_SESSION_OPEN`, `TERMINAL_LEG_POLICY_V1`
  and `SIMPLE_10BPS_PER_SIDE_V1`.
- **Frozen on the mechanism, explicitly not on performance.** The record states
  the reason (C1–C4 met under a rule fixed before execution) and the
  non-reason (+1.35 pp over 17 rebalances on one path, Sharpe marginally lower,
  noise) in the same block, with `performance_entered_the_verdict: false`.
- **Historical development CLOSED**: parameter search `CLOSED`; additional
  retention widths, frequencies, weighting rules and holding-period rules all
  `PROHIBITED`; `historical_clean_holdout` `NONE`. Twelve prohibited
  modifications enumerated under this candidate id.
- **Protocol `H2_PROSPECTIVE_SHADOW_PROTOCOL_V1`**, frozen and inactive. A
  mechanism confirmation study, not an alpha one. Window: 5 decisions, 4
  transitions, with `stop_early_on_success`, `extend_on_unfavourable_result`,
  `choose_best_four_afterwards`, `drop_an_unfavourable_transition` and
  `replace_an_outage_transition_retrospectively` all false.
- **P1 and P2 are paired and directional** against the comparator run on the
  same prospective cross-sections; `historical_magnitude_required: false` on
  both, and the historical figures are carried under
  `historical_values_are_references_only` with
  `any_of_these_is_a_prospective_threshold: false`. The 28.23% reduction and the
  legacy 1.95× are references, never thresholds. P3 preserves exposure; **P4
  makes protocol integrity a pass condition** by counting retrospective packet
  replacement as a violation. Aggregate `P1 AND P2 AND P3 AND P4`, no weighting,
  no majority, no partial label.
- **No early verdict is representable**: `interpret_prospective` returns
  `PENDING` with every criterion `PENDING` until four transitions are in, and
  `evaluate_prospective` takes no performance argument.
- **First eligible decision is a rule**: `NOT_YET_ASSIGNED`, `hardcoded_date:
  null`, derived at activation from the benchmark calendar and the requirement
  that the runner be armed by the preceding session's close. A missed quarter is
  skipped; a reconstruction carries `RETROSPECTIVE` and
  `NOT_PROSPECTIVE_CONFIRMATION` and never enters the criteria.
- **`PROSPECTIVE_SHADOW_CONTRACT_V1`** bridges the frozen semantics to a live
  stream explicitly — decision-time availability at 16:00 America/New_York with
  the direction-of-error asymmetry named, identity key, feature timestamps,
  designated price source, `UNPRICED` with no substitution, corporate actions,
  terminal treatment, `POST_DECISION_CORRECTION` never rewriting a frozen
  packet, and storage in `PROSPECTIVE_SHADOW_STREAM_V1` which must not write
  into `PIT_RESEARCH_DATASET_V2`.
- **Three schemas written before any decision exists.** The decision packet is
  `additionalProperties: false` with no outcome field — a packet carrying a
  forward return fails its own schema — and immutability is by key and digest.
  The ledger classifies each decision's code change and caps completed
  transitions at 4. The evaluation schema pins
  `performance_entered_verdict: false` as a constant.
- **Promotion ladder frozen**: `FROZEN_H2_CANDIDATE` (current) →
  `PROSPECTIVE_SHADOW` (R1.7) → `BROKER_PAPER`, `SUPERVISED_SMALL_LIVE`,
  `CONSTRAINED_AUTONOMOUS`, the last three `NOT AUTHORISED`. Shadow, paper and
  live are distinguished in the record so none is mistaken for another.
- **Determinism**: 8 semantic fingerprints identical across two runs; 10 files
  mirrored to `control/PIT_RESEARCH_DATASET_V2/research/r1-6/` and each read
  back and verified. Digests: `h2_candidate_freeze.json` `a7ace1d1…`,
  `h2_prospective_protocol_v1.json` `60c17136…`, `r1_6_complete.json`
  `ba028a9f…`.

### R1.5 preregistered H2 experiment executed — 2026-09-25

- `backtest run-h2 --runs 2 --mirror` with all four `--expect-*` digests:
  database `20a77d24…`, contract `f7efb001…`, H2 record `56f2e51f…`, R1.5 spec
  `6d266448…`. **38 binding checks** — the whole R1.0/R1.2 record re-derived,
  R1.3 unchanged, then `verify_h2` on the preregistration itself. The
  comparator's stored turnover was matched to 1e-12 so the experiment could not
  be measured against a rebuilt variant.
- **One variant, one run.** `r1_5_execution_state.json` was written before the
  run began; a second invocation is refused by digest and timestamp. The
  preregistered `r1_5_experiment_spec.json` still hashes to `6d266448…` — it was
  not rewritten to carry a result.
- **Order enforced structurally.** Bind → decisions → memberships frozen to disk
  → simulate → accounting → turnover → **mechanism verdict written** →
  performance. `evaluate` has no parameter a return could arrive in, and a test
  asserts its signature.
- **Shared with the comparator by construction**: the calendar, the executable
  cross-sections, the scores and the target counts are R1.2's own. The first
  selection reproduced R1.2's 74 names exactly, and all 17 target counts matched
  its selection ledger.
- **C1 MET** — annual turnover **1.6177403954997098×** against a threshold of
  2.253982668222645×, a 28.2% reduction. **C2 MET** — entry/exit churn turnover
  **1.411598539883737×** against 2.11411510465003×. **C3 MET** — target counts
  identical at all 17 decisions, 0 intentional cash quarters, no
  hysteresis-caused exposure reduction. **C4 MET** — 0 accounting, 0 identity, 0
  leakage, 0 same-close violations. **Aggregate `C1 AND C2 AND C3 AND C4` →
  SUPPORTED.**
- **Decomposition reconciles exactly**: entry 0.796895, exit 0.614704, resizing
  0.206142; unexplained turnover **0.0**, shares summing to 1.0 within 1e-12.
  Per-rebalance turnover mean 0.404435, median 0.397570, max 0.500000.
- **Attribution of the 0.636242× reduction**: entry −0.344063, exit −0.363271,
  resizing **+0.071092**. Resizing rose — holding names longer leaves more weight
  drift to correct — and the sign is reported rather than suppressed; residual
  0.0.
- **Membership moved as the hypothesis said it should**: retention 45.77% →
  **64.40%** (+18.63 pp), holding duration mean 1.73 → **2.48** quarters (median
  2, p90 5, max 17), re-entry 35.35% → **23.57%** of episodes (109 identities,
  128 events). 543 entrants, 442 exits (82 forced by the contract, 360 out of
  band, 0 over capacity), 240 band retentions. Mean Jaccard against the ordinary
  set **0.707**.
- **Displacement measured, not assumed**: 240 pairs, mean rank difference 48.05
  positions (median 46), mean score difference 3.2174 points.
  `pairing_used_forward_labels: false`.
- **Performance, computed afterwards and deciding nothing**: gross +127.90%
  (CAGR 22.52%, vol 23.59%, Sharpe 0.9793, Sortino 0.9454, max drawdown
  −23.64%, Calmar 0.9527) against the comparator's +126.55% / 22.34% / 0.9810 /
  −24.16%, and SPY's +67.63%. Benchmark-relative **+60.3 pp**; net at 10 bps
  +125.07% with a 2.83 pp drag; break-even **222.57 bps per side** (comparator
  156.9). Profit $127,135.34, reconciliation residual −1.7e−10.
- **Signal diagnostics, descriptive only**: selected-set forward return 2.45% vs
  a 1.60% cross-section mean at 20 sessions (+0.85 pp), 5.78% vs 2.88% at 60
  (+2.91 pp), 13.05% vs 6.21% at 120 (+6.84 pp). Average selected score 66.53;
  selected rank percentile mean 0.0596, median 0.0492, p90 0.1334, max 0.1989 —
  inside two selection widths by construction.
- **Legacy blocks, `DESCRIPTIVE_ONLY_FOR_H2`**: `LEGACY_DEV_BLOCK` +28.39% vs
  +19.16% (+9.2 pp); `LEGACY_VAL_BLOCK` +0.89% vs +8.27% (**−7.4 pp**);
  `LEGACY_HOLDOUT_BLOCK` +75.94% vs +29.94% (+46.0 pp), additionally
  `CONTAMINATED_FOR_H2_CONFIRMATION`.
- **Determinism**: 13 semantic fingerprints identical across two runs; 9 files
  mirrored to `control/PIT_RESEARCH_DATASET_V2/research/r1-5/` and each read back
  and verified. Run manifest schema-valid, `production_status: NOT_PRODUCTION`.
  Digests: `r1_5_complete.json` `9fb0f28b…`, `r1_5_h2_evaluation.json`
  `4d006e50…`, `r1_5_summary.json` `0a169a93…`.
- **Confirms nothing.** Every artifact carries `EXPLORATORY_HISTORICAL_TEST` and
  `independent_confirmation: NOT_PROVIDED_BY_R1_5`; 0 clean-holdout evaluations,
  0 historical confirmation claims, 0 alternative widths, 0 alternative
  frequencies, 0 sweeps.

### R1.4 H2 membership-stability hypothesis preregistered — 2026-09-25

- `backtest preregister-h2 --runs 2 --mirror` with `--expect-contract-sha`
  `f7efb001…` and `--expect-database-sha` `20a77d24…`. **14 binding checks**
  passed, including a re-read of every R1.3 measurement H2 rests on: quarterly
  annual turnover 2.253982668222645×, membership floor 2.1242318895×, quarterly
  retention 45.7707%, monthly three-month survival 45.9757%, ranking-churn share
  93.7946%, resizing share 5.9780%, 0 implementation and 0 accounting defects.
  Drift in any of them refuses the phase.
- **0 strategies simulated, 0 return calculations, 0 turnover calculations.**
  `simulation_reachable` scans the package's imports for `simulate`,
  `PriceBook`, `Rebalance`, `performance`, `benchmark_path`, `run_experiment`,
  `run_r1_0` and `load_labels`, and returned empty. `research_ledger.jsonl`
  records `HYPOTHESIS_PREREGISTRATION`, 0 variants tried, 0 sweeps, 0 holdout
  evaluations, `turnover_calculated: false`.
- **One hypothesis, one rule.** `H2_MEMBERSHIP_STABILITY` /
  `ONE_DECILE_HYSTERESIS_V1`: entry at the ordinary top-decile target count,
  retention while rank position is inside twice that count, target size taken
  from R1.2 and never recomputed, equal weight, next-session open, calendar
  quarter ends. The first rebalance has no incumbents and is therefore exactly
  R1.2's selection. Forced exits — ineligibility, missing score, failed
  tradability, missing execution eligibility, terminal state — override the band.
- **Both numbers are structural, not selected.** The band is two selection
  widths expressed in names, which equals the top 20% by construction while
  inheriting the entry rule's rounding rather than adding a second one. No
  alternative width was evaluated, and `PROHIBITED_VARIATIONS` names twelve
  variations a later phase may not make while calling itself H2.
- **Four frozen criteria** measured against the comparator
  `COMPOUNDER_QUARTERLY_TOP_DECILE_V1`: C1 annual turnover < 2.253982668222645×;
  C2 annualised entry/exit churn turnover < 2.11411510465003× (absolute, not a
  share); C3 same target-count rule, 0 intentional cash quarters, no
  hysteresis-caused exposure loss; C4 zero accounting, identity, leakage and
  same-close violations. **Aggregate rule `C1 AND C2 AND C3 AND C4`, stated
  before execution** — the one deliberate difference from H1, which stated none.
- **1.95× is not an H2 criterion.** Carried as
  `LEGACY_H1_TURNOVER_REFERENCE`, a continuity reading only, on R1.3's finding
  that it is an empirical anchor times an undeclared one half. Not moved. The
  binder refuses a specification that promotes it or changes its value.
- **Contamination recorded, not worked around.** The frozen dataset is
  `HYPOTHESIS_GENERATION_DATA_FOR_H2`; all three blocks were exposed; a later
  historical run must be classified `EXPLORATORY_HISTORICAL_TEST` and may not be
  called independent, clean-holdout, out-of-sample or prospective confirmation;
  `clean_h2_holdout_remains` false; `holdout_reset` false;
  `CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST` true; prospective duration
  `null`, because no research policy defines one.
- **The record carries the evidence against itself**: only 34.2% of membership
  changes move a name under ten rank-percentile points while 37.6% move it over
  twenty, so a band may catch less churn than its motivation suggests; 13.0% of
  exits and 24.4% of entries are availability changes no retention band can
  prevent; and retaining a name the ordinary rule would sell means not holding a
  higher-ranked one, at an unmeasured cost.
- **`verify_h2` is the gate R1.5 must pass**, written now rather than later. It
  refuses a moved record or specification, a specification bound to a different
  record, one already executed or permitting more than one run, more than one
  value in any of eight singular fields, any sweep or optimisation, a stored or
  executor rule that drifted, changed criteria or aggregate rule, a promoted or
  moved 1.95×, a wrong comparator, and any claim of a clean holdout or a
  relabelled subperiod. No `--force`.
- **Determinism**: 7 semantic fingerprints identical across two runs; 5 files
  mirrored to `control/PIT_RESEARCH_DATASET_V2/research/r1-4/` and each read back
  and verified. Digests: `h2_membership_stability.json` `56f2e51f…`,
  `r1_5_experiment_spec.json` `6d266448…`, `r1_4_complete.json` `66629f3f…`.
- **No H2 performance is recorded anywhere, because none exists.**

### R1.3 failed turnover premise explained — 2026-09-25

- `backtest review-turnover --runs 2 --mirror` with `--expect-contract-sha`
  `f7efb001…` and `--expect-database-sha` `20a77d24…`. **27 binding checks**
  passed: the contract, every R1.0 ledger fingerprint, all 14 R1.2 artifact
  fingerprints, the H1 record `a64268ef…`, the R1.2 specification `1b30916d…`,
  and R1.2's interpretation being `PARTIALLY_SUPPORTED_BY_R1_2` with C3 the only
  unmet criterion. Both databases byte-identical before and after.
- **0 strategies simulated.** `simulation_reachable` scans the forensic
  package's own imports and returned empty; a non-empty result ends the command.
  `research_ledger.jsonl` records `FORENSIC_ANALYSIS`, 0 variants tried, 0
  sweeps, 0 new frequency or selection variants, 0 new holdout evaluations.
- **Turnover reproduced from the ledgers alone**, bit for bit: quarterly
  2.253982668222645× against the stored 2.253982668222645×, discrepancy exactly
  0.0. The monthly C3 anchor reproduced at 3.895762988882577× against
  3.8957629888825784×, differing in the last representable place. Pre-trade NAV
  was recovered as `market_value / actual_weight`, which under the gross cost
  model equals the pre-trade figure.
- **Decomposition closes exactly**: entrants 50.5046%, exits 43.2901%, resizing
  5.9780%, terminal 0.2274% — shares summing to 1.0 within 1e-12, ranking churn
  93.7946%, residual 0.0 on 2,471,817.294027481 of rebalance notional and 0.0 on
  5,632.624902353189 of terminal notional. 0 accounting violations, 0 unpriced
  selections, 0 quarters in cash.
- **Frequency decomposition.** Monthly matched 49 rebalances at 0.31368215
  per-event (3.7642× nominal); quarterly 17 at 0.56349567 (2.2540×). Ratio
  **1.7964**; the counterfactual holding per-event turnover at the monthly level
  gives **1.2547×**, so **39.8%** of the frequency benefit was given back.
  Required reduction to reach 1.95×: 0.303983× absolute, 13.49% relative.
- **Lower bound — the decisive measurement.** Removing every resizing trade
  while holding the executed selections fixed leaves **2.1242×**, above the
  ceiling by 0.1742×. `could_same_selections_satisfy_threshold: false`, so C3
  was unreachable by frequency and weighting mechanics alone →
  **`H1_MECHANISTICALLY_FALSIFIED`**, mechanism
  `M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT`.
- **Retention.** Quarter-to-quarter 45.77% (median 46.48%, min 38.55%, max
  58.90%) against the monthly survival curve's 71.46% / 56.41% / **45.98%** at
  one, two and three months — the same quantity over the same elapsed time,
  agreeing to 0.2 pp, and already recorded in H1's premise B. Interpolated
  membership half-life **2.61 months**, slower than the 2.06 a geometric decay
  from the first step would give, so a persistent core sits inside a churning
  fringe.
- **Membership change by cause.** Exits (677): ranking 87.00%, tradability
  6.20%, score readiness 3.40%, not in universe 3.10%, feature freshness 0.30%.
  Entries (704): ranking 75.57%, tradability 16.19%, score readiness 4.12%, not
  in universe 3.98%, feature freshness 0.14%.
- **Churn is not boundary noise.** Rank moves: under 5 points 14.38%, 5–10
  19.84%, 10–20 28.20%, over 20 **37.58%**; median move 0.1488, mean 0.2076.
  Entrants' prior band: top 5% 5.87%, 5–10% 4.17%, 10–15% 23.03%, 15–20% 17.31%,
  beyond 20% **49.61%**. Exits' current band: beyond 20% **57.50%**.
- **Rank and score persistence**, measured on R1.0's monthly cross-sections.
  All-scored Spearman 0.9066 / 0.8129 / 0.7323 at one, two and three months
  (≈1,397 identities per pair); executable-only 0.9089280237618296 / 0.8182 /
  0.7366 (≈719 per pair), the first of which reproduces R1.1's recorded
  month-to-month correlation exactly. Mean absolute score change 2.73 / 4.55 /
  5.90 points on the executable population, p90 6.72 / 10.73 / 13.10.
- **Holding duration**, 778 episodes over 503 identities: mean 1.7314 quarters,
  median 1, p75 2, p90 4, maximum 14; 504 single-quarter episodes. Re-entry: 202
  identities, 275 events, 35.35% of all episodes, median 2 quarters outside.
- **Turnover spread, not concentrated.** Largest rebalance 10.03% of traded
  notional (2026-04-01), top 3 26.89%, top 5 41.89% against an even share of
  5.88%. By block: DEV 2.1769×, VAL 2.2601×, HOLDOUT 2.3724×. By year: 2022
  1.8809× (partial), 2023 2.3545×, 2024 2.2140×, 2025 2.4540×, 2026 2.2924×
  (partial). Dispersion-versus-turnover Spearman −0.4363 across 17 observations.
- **Threshold provenance.** C3's 1.95× is a product of two parts of different
  kinds: an empirical anchor (3.90×, R1.0's full-window monthly turnover, but
  measured after R1.0 had read its holdout, so not pre-holdout) times an
  arbitrary round reduction factor (one half, with no derivation anywhere in the
  record — no cost model connects it to premise D's 77.9 bps). Classified
  `OTHER`, and not altered.
- **Two reporting notes, recorded not fixed.** R1.2's comparator field
  `rebalances_in_window` reads 50 where 49 is correct, because it counts distinct
  month labels and a terminal settlement carries an empty label; it feeds no
  criterion and no turnover figure (`COSMETIC_METADATA`). And the frozen record
  annualises turnover two ways — nominal frequency for the quarterly figure and
  C3's anchor, elapsed years for the matched comparator — which is why that
  comparator reads 3.7949× where nominal gives 3.7642× for the same window. C3's
  verdict is invariant, and the convention used understates quarterly turnover
  (17 rebalances over 4.056 years is 4.19 a year, not 4.00).
- **Determinism**: 11 semantic fingerprints identical across two runs; 11 files
  mirrored to `control/PIT_RESEARCH_DATASET_V2/research/r1-3/` and each read
  back and verified. Digests: `r1_3_complete.json` `89e085c1…`,
  `r1_3_summary.json` `467ab0c4…`, `r1_3_h1_forensic_evaluation.json`
  `77c8c993…`, `r1_3_turnover_lower_bound.json` `9dac937d…`.
- **Holdout untouched.** Still `CONTAMINATED_FOR_H1_CONFIRMATION`, not upgraded,
  0 new holdout strategy evaluations. No aggregate rule invented: R1.2's
  four-of-five remains `PARTIALLY_SUPPORTED_BY_R1_2`. Next: **R1.4 — define the
  next preregistered hypothesis**, dimension
  `MEMBERSHIP_STABILITY_OF_THE_SELECTED_SET`, numeric rule **none chosen**.

### R1.2 preregistered quarterly experiment executed — 2026-09-24

- `backtest run-quarterly --database stage9k.db --runs 2 --mirror` with all
  eight `--expect-*` digests: database `20a77d24…`, contract `f7efb001…`,
  experiment spec `1b30916d…`, hypothesis `a64268ef…`, R1.1 marker `fb1dc4df…`,
  R1.0 marker `544555c6…`, split `b58283be…`, preregistration `8a19f0fd…`. All
  13 frozen rules matched the executor; 18 R1.0 fingerprints re-derived
  underneath. Two runs, **15 fingerprints identical**, 9 files mirrored under
  `control/PIT_RESEARCH_DATASET_V2/research/r1-2/`, both databases unchanged,
  run manifest schema-valid, zero accounting violations, profit reconciled to
  −7.3e−11.
- 17 quarter ends 2022-03-31 … 2026-03-31 (8 development, 4 validation, 5
  observed-holdout); valuation window 2022-04-01 … 2026-04-30, 1,023 sessions;
  56,562 universe rows sliced from R1.0, 13,533 executable (23.9%), 1,347
  positions selected over 17 rebalances across 503 identities, 22 of them
  historical. No quarter held cash; no selected name went unpriced.
- **Quarterly gross:** +126.5% total, CAGR 22.3%, volatility 23.3%, Sharpe
  0.98, Sortino 0.95, max drawdown −24.2%, Calmar 0.93, average holdings 78.2,
  cash utilisation 99.9%, annual turnover 2.254×. Under
  `SIMPLE_10BPS_PER_SIDE_V1`: +122.7%, a cost drag of 3.9 pp.
- **SPY over the same sessions:** +67.6%, CAGR 13.6%, Sharpe 0.83, max drawdown
  −21.3%. Benchmark-relative +58.9 pp.
- **Matched monthly comparator** (R1.0's frozen ledger sliced to those
  sessions, 50 rebalances): +103.5%, CAGR 19.1%, Sharpe 0.88, max drawdown
  −25.9%, annual turnover 3.795×, net-10bps +97.5%. **Quarterly minus monthly:**
  total +23.1 pp, CAGR +3.2 pp, Sharpe +0.10, max drawdown +1.7 pp (shallower),
  turnover −1.54×, cost drag −2.1 pp, holdings +1.1.
- **Blocks** (quarterly / SPY / relative): development +28.9% / +19.2% / +9.8 pp
  over 8 rebalances; validation +1.0% / +8.3% / **−7.3 pp** over 4;
  observed-holdout +74.1% / +29.9% / +44.1 pp over 5 — the last labelled
  `CONTAMINATED_FOR_H1_CONFIRMATION`.
- **Years** (quarterly / SPY): 2022 partial −9.3% / −14.5%; 2023 +22.9% /
  +26.2%; 2024 +26.3% / +24.9%; 2025 +40.6% / +17.7%; 2026 partial +14.5% /
  +5.7%.
- **Quarter-end signal** (D10−D1 / D10−cross-section mean / rank IC / decile
  association): 20 sessions −0.10 pp / +0.45 pp / +0.020 / +0.27; **60 sessions
  (primary) +2.02 pp (t 1.28) / +2.55 pp / +0.036 / +0.479**; 120 sessions
  +6.43 pp (t 3.72) / +6.14 pp / +0.067 / +0.721. The profile is `U_SHAPED` at
  every horizon, and the 120-session window overlaps adjacent quarters.
- **Turnover decomposition:** 93.8% ranking churn (entrants 50.5%, exits
  43.3%), resizing 6.0%, terminal 0.2%. Quarter-to-quarter retention 45.8%;
  mean holding duration 1.73 quarters, maximum 14.
- **Concentration:** largest contributor 4.9%, top 5 21.2%, top 10 34.3%, top 25
  61.3%, top 50 90.4%. Per-side cost break-even **156.9 bps** against the
  monthly 77.9 bps.
- **Path:** worst relative drawdown −13.9% on 2023-11-09; 706 sessions below a
  prior relative peak; 315 of 771 rolling twelve-month windows behind SPY.
- **H1 criteria:** C1 met (+0.0202 > 0); C2 met (+0.479 > 0); **C3 not met
  (2.254× against the ≤1.95× threshold)**; C4 met (2 of 3 blocks positive, at
  the threshold); C5 met (21.2% < 50%). Outcome
  `PARTIALLY_SUPPORTED_BY_R1_2`. The specification states no aggregate rule and
  none was invented.
- Current-classification limitation on the quarterly sample: 99.0% of
  executable observations, 98.4% of selections, 98.3% of portfolio weight.
  Three terminal legs settled, none unknown.

### R1.1 Compounder baseline signal interpreted, one experiment specified — 2026-09-24

- `backtest review-signal --database stage9k.db --runs 2 --mirror` with all
  five `--expect-*` digests: the database re-hashed to `20a77d24…`, the
  contract to `f7efb001…`, the R1.0 completion marker to `544555c6…`, the split
  to `b58283be…` and the preregistration to `8a19f0fd…`; **18 R1.0 fingerprints
  re-derived and matched** before any statistic was computed. Two runs, 13
  fingerprints identical, 13 files mirrored under
  `control/PIT_RESEARCH_DATASET_V2/research/r1-1/`, both databases unchanged.
  No strategy executed.
- Recomputing R1.0's own diagnostics reproduced them exactly — overall mean
  monthly rank IC +0.02515 against R1.0's recorded +0.0251 — which is the
  cross-check that the review reads the same record.
- **Signal by split** (D10−D1 / D10−cross-section mean / rank IC): development
  +0.87 / +0.61 / +0.026 pp; validation +1.56 / +1.06 / +0.042; **R1.0 holdout
  +0.06 / +0.55 / +0.011**. The holdout's decile profile is `U_SHAPED` — D1
  +1.65%, a trough of +0.28% at D6, D10 +1.72% — which is why the long-short
  contrast collapses there while the long-only measure does not.
- **By year** (D10−D1 / rank IC / positive-IC months): 2022 +1.27 pp / +0.006 /
  7 of 12; 2023 +0.66 / +0.046 / 7 of 12; 2024 +1.62 / +0.042 / 10 of 12; 2025
  +0.38 / +0.015 / 7 of 12; **2026 (3 months) −1.47 pp / −0.006 / 1 of 3**.
- **By horizon**, both measures and the IC rise monotonically: rank IC +0.017
  (1 session), +0.023 (5), +0.025 (20), +0.037 (60), +0.063 (120); D10−D1
  +0.11, +0.08, +0.84, +2.65, +5.75 pp. The 60- and 120-session windows overlap
  about two and five later monthly decisions and are flagged as non-independent
  wherever they are quoted.
- **Robustness.** Leave-one-month-out: both headline means stay positive in all
  51 removals (D10−D1 ranges +0.71 to +1.02 pp; IC +0.021 to +0.030).
  Leave-one-year-out: positive in all 5 removals (+0.60 to +0.98 pp).
- **Persistence.** Month-to-month rank correlation 0.909; top-decile retention
  71.5% at one month and 46.0% at three.
- **Concentration.** Profit $120,671.94, attributed identity by identity with a
  reconciliation residual of −2.8e−10. Largest contributor 5.6%; top 5 20.4%;
  top 10 30.8%; top 25 56.5%; top 50 86.4% (of 578 identities, 307 winners).
  Best month 18.4% of profit, best three 44.1%, best five 66.5%.
- **Turnover** is 88.7% ranking churn — 47.0% entrants, 41.7% exits, 11.1%
  resizing, 0.1% terminal — which is the fact that makes a lower-frequency
  experiment worth running.
- **Cost.** Realised traded notional over NAV sums to 33.16 (7.85× a year). The
  analytic model reproduces the one level R1.0 ran to within 0.3 pp on a +110%
  return, and gives a per-side break-even against SPY of **77.9 bps**. Spread,
  impact, capacity and latency are not in that number.
- **Path.** The strategy never closed below its starting relative level, but
  spent 736 sessions below a prior relative peak; worst relative drawdown
  −13.5% on 2025-03-10; 310 of 813 rolling twelve-month windows behind SPY.
- **Population.** 164,193 universe observations → 39,001 executable (23.8%).
  By class: COMPANY 24.5% reach executable, HISTORICAL **5.9%** — the attrition
  is at score-readiness (25.8% vs 46.4%) and tradability, not at universe
  construction. Historical identities are 12 of 578 contributors and 0.07% of
  profit.
- **Limitations quantified.** The undated-classification exposure covers 99.0%
  of executable observations, 98.3% of top-decile selections and 98.2% of
  portfolio weight-months. The score floor holds 23 observations across 10
  identities and 14 months (MARA 7, WULF 3, RXRX 3, SOFI 3, LCID 2, and five
  once each), all `SCORED` — a scale floor, not a parser defect.
- **Verdict.** `STRONG_ENOUGH_FOR_HYPOTHESIS_TEST` on all eight rubric
  conditions; `H1_MEDIUM_HORIZON_SELECTION` accepted with all five premises
  `SUPPORTED`; `COMPOUNDER_QUARTERLY_TOP_DECILE_V1` specified
  (`1b30916d7b131ce8…`) and **not executed**.

### R1.0 backtest harness built and first frozen baselines executed — 2026-09-23

- `backtest run-baselines --database stage9k.db --verify-fingerprints
  --runs 2 --workers 8 --mirror` with all eleven `--expect-*` digests: the
  research database re-hashed to `20a77d24…` before and after, the contract
  file to `f7efb001…`, `pit_coverage_v2.jsonl` to `1fc94fc1…` and
  `pit_month_outcomes.jsonl` to `a955a2b7…`; all fourteen dataset projections
  recomputed and matched (`historical_universe 19b86e63…`,
  `price_history 0205e3b6…`, `historical_prices c53a806e…`,
  `signal_observations 79730d0d…`, `forward_return_labels a997311a…`, and the
  other nine); `load_contract` accepted the bindings.
- 51 decision dates derived from readiness (2022-01-31 … 2026-03-31);
  valuation window 2022-02-01 … 2026-04-30, 1,065 sessions. The holdout ends
  at 2026-03 because point-in-time features exist for both identity classes
  only through that month; the boundary was set before any performance.
- Funnel over 250,537 frozen universe rows: 164,193 admitted (157,526 COMPANY,
  6,667 HISTORICAL) → 163,850 feature-ready → 74,829 scored → 74,193 rankable
  → 39,003 tradable → **39,001 executable** (38,609 COMPANY, 392 HISTORICAL).
  Every exclusion carries a reason and is counted.
- Gross, whole window: `BASELINE_PASSIVE_V1` +68.2% (CAGR 13.10%, vol 17.5%,
  Sharpe 0.79, Sortino 0.78, max DD −22.1%, Calmar 0.59);
  `BASELINE_UNIVERSE_EQUAL_WEIGHT_V1` +43.5% (CAGR 8.94%, Sharpe 0.52, max DD
  −24.0%, 766.1 average holdings, annual turnover 1.38×, benchmark-relative
  −24.65 pp); `COMPOUNDER_TOP_DECILE_V1` +117.7% (CAGR 20.24%, Sharpe 0.92,
  max DD −25.8%, Calmar 0.78, 76.1 average holdings, annual turnover 3.90×,
  benchmark-relative +49.58 pp). Under `SIMPLE_10BPS_PER_SIDE_V1`: +42.0% and
  +110.9%.
- By split, gross: development +5.2% / +21.3% (equal-weight / top-decile)
  against SPY +10.0%; validation +15.5% / +33.9% against +26.2%; holdout
  +18.2% / +34.1% against +21.1%.
- `COMPOUNDER_DECILES_V1`: 39,001 observations, 51 cross-sections, all cut. At
  20 sessions, D1 … D10 means +0.56, +0.60, +0.26, +0.39, +0.28, +0.38, +0.37,
  +0.91, +1.12, +1.31 percentage points, with mean score rising 25.4 → 67.1.
  `D10 − D1` pooled +0.75 pp; averaged over months +0.84 pp, standard error
  0.51 pp, t = 1.66, bootstrap CI [−0.15, +1.79] pp, positive in 58.8% of
  months. Mean monthly rank IC +0.025 (median +0.028, std 0.102, t = 1.76, CI
  [−0.003, +0.053], positive in 62.7% of months); by split +0.026 development,
  +0.042 validation, **+0.011 holdout**.
- Accounting: zero violations, zero cash violations, zero empty
  cross-sections, zero selected securities without an execution price, 2
  observations excluded at `NO_EXECUTION_PRICE`. 33 terminal legs resolved by
  identity — 30 settled at a stated consideration, 3 unknown; valuing every
  unknown leg at nothing costs the equal-weight path 0.19 pp and the
  top-decile path nothing. 121 buy orders on the equal-weight baseline were
  clamped by the cash guard (unsaleable carried positions plus floating-point
  residue), leaving 10 selections unfilled out of 39,001.
- Determinism: two independent executions, 37 semantic fingerprints identical,
  including trades, holdings, NAV, metrics, diagnostics and both audits.
- Audits: the hash-seeded spot audit re-derived 24 decisions across three
  sampled months (2023-11, 2024-12, 2025-05) with zero failures, reproducing
  each score to the digit; the bias audit shows historical identities at 4.1%
  of the universe and 1.0% of the executable set, with realised holdings at
  1.1% (equal-weight) and 1.7% (top-decile) — under-represented, reported, not
  corrected for.
- Outputs under `.evidence/research/r1_0/`; 19 compact files mirrored as
  snapshots under `control/PIT_RESEARCH_DATASET_V2/research/r1-0/` and read
  back. Both databases byte-identical before and after; no migration; no SEC
  request; no evidence write.

### B2.18 research dataset frozen and backtest contract written — 2026-09-19

- `evidence freeze-backtest-contract --database stage9k.db
  --expect-database-sha 20a77d24… --expect-universe-sha b8eaed3e…
  --expect-manifest-sha a5653f1c… --expect-index-sha e6ce97d6…
  --expect-pit-marker-sha e5f3bbbd… --expect-audit-marker-sha ae49ae16…
  --expect-package-sha 79f644f1… --expect-plan-sha 0efa7d03…
  --also-verify compounder_radar.db=02a6660b… --fingerprint-runs 2`, 20:05–20:19
  UTC: seven bound files re-hashed before and after; the universe re-proven
  read-only; the dataset fingerprinted twice with identical results; the
  contract built twice with identical content; the written contract loaded
  under its own bindings and refused under altered ones; 8 files mirrored
  under `backtest-contract/` (16 objects) and read back; 0 SEC requests, 0
  evidence reads or writes; both databases unchanged.
- Dataset fingerprints (`backtest_dataset_manifest.json`): historical_universe
  268,780 `19b86e63…` · universe_membership 20,412 `2e0e81a7…` ·
  security_identifiers 3,482 `1e1d21a8…` · fundamental_observations 5,782,076
  `4c564ec5…` · filing_acceptance 5,889,860 `bd5cafe4…` ·
  historical_observations 321,534 `862ef3ce…` · price_history 6,817,179
  `0205e3b6…` · historical_prices 1,466,908 `c53a806e…` · benchmark_prices
  1,531 `f900e279…` · corporate_actions 82,321 `cc041593…` · delistings 5,796
  `4709b426…` · feature_observations 5,789,830 `0531abf4…` ·
  signal_observations 6,153,527 `79730d0d…` · forward_return_labels
  31,324,895 `a997311a…`; PIT evidence state carried as the B2.16 package
  fingerprints (month outcomes 242,269 `a955a2b7…`).
- Contract files: data contract `f7efb001…`, dataset manifest `85de2089…`,
  readiness matrix `f47adaf8…`, time semantics `dedc8a2e…`, feature/label
  separation `526d320e…`, run-manifest schema `15ab5429…`, readiness summary
  `47b5dc2b…`, completion marker `14a45f77…`.
- Readiness: READY_WITH_DOCUMENTED_LIMITATIONS. READY: sec_pit_evidence,
  market_signals, forward_returns. READY_WITH_DOCUMENTED_LIMITATION:
  historical_universe (MEMBER always INFERRED), fundamental_features
  (historical securities select by `filed_date <= as_of`; research applies
  `filed_date < as_of`), compounder_score (exchange/sector/statement profile
  from the current row), price_history (single venue, adjusted, from 2020-02
  for companies), corporate_actions (spinoffs unsupported),
  liquidity (one-directional floors), delisting_treatment (INTERRUPTED
  carries no number; 291 of 3,150 terminal values). BLOCKED: sector_industry
  (current labels only, 623/5,769; minimum fix: a dated sector source or
  research declares sector NOT USED). PARTIAL: transaction_costs (no spread,
  no consolidated ADV). NOT_REQUIRED_FOR_FIRST_BASELINE: portfolio_constraints.
- Audits recorded in the contract: label layer imported by none of the ten
  feature/scoring modules (static test); latest-value queries in historical
  paths are RECORD_EXTENT (`MAX(price_history.date)` to adjudicate delistings
  and bound membership) or bounded by `as_of`; current metadata reaches only
  eligibility (`CURRENT_APPLIED_BACKWARD`); 69 contaminated live rows
  reported, never merged. Universe: 5,287 LIVE and 1,237 DELISTED identities
  with open months; 655,095 of 31,324,895 label rows and 119,516 of 5,789,830
  feature rows belong to historical securities — the dataset is not today's
  roster applied backward, and the delisted names' lower coverage (2,569 of
  3,150 with bars; 402 with a CIK) is stated, not removed. Same-day filings:
  a 15:55 ET acceptance is admitted at the close, a 17:30 one at the next
  session, and an OPEN decision admits neither (canary).
- Recovery from the bucket alone: which contract applies, which dataset is
  frozen, which generation it derives from, what is allowed at decision
  time, what is forbidden, which hashes define the dataset, which
  limitations exist, and the evidence lineage — all read back and equal:
  `BACKTEST_CONTRACT_RECOVERY_SUFFICIENT = YES`. Credential scan over 24
  files: 0.
- `tests/integration/test_backtest_contract.py` (26): deterministic build
  naming every file; fail-closed load on every binding and on a tampered
  file; the readiness matrix naming a fix for every blocker; the run-manifest
  schema requiring the bindings and the selection population; canonical
  fingerprints covering every layer; the mirror; and the canaries — future
  filing (instant-level, same-day, OPEN vs CLOSE, next session), restatement,
  future price (label moves, feature does not), forward label (static import
  guard over ten modules), ticker reuse (two occupants of BBBY), CIK/class
  (SAFE_COMMON_CLASS credits the common line only), delisted security (months
  kept without a companies row), missingness.

### B2.17 residual PIT audit — 2026-09-19

- `evidence audit-pit --expect-index-sha e6ce97d6… --expect-manifest-sha
  a5653f1c… --expect-pit-marker-sha e5f3bbbd… --expect-package-sha
  79f644f1… --expect-plan-sha 0efa7d03… --expect-universe-sha b8eaed3e…
  --runs 2`, 18:37 UTC: both databases unchanged before and after; the
  B2.16 package re-proven from its fingerprints (242,269 rows, 0
  duplicates, 155,620 + 86,649, 0 regressed, 0 future-invalid); two runs,
  every fingerprint identical; 0 SEC requests, 0 evidence reads or writes;
  8 small files mirrored under `pit-audit/` (16 objects), read back equal;
  the residual rows and the forensic plan not mirrored.
- Planning realization (exact cells): solvable→resolved 155,040 ·
  solvable→unresolved 9,532 · unsolvable→resolved 580 · unsolvable→unresolved
  77,117 = 242,269; realization 94.21%, surprise 0.75%; expected 155,114.479
  vs actual 155,620 (+505.5, +0.33%). Transition matrix G1/G2/G3/G4/G5 →
  RESOLVED/G1/G2/G3/G4/G5 recorded in `pit_residual_summary.json`.
- Residual 86,649 — final classes G1 8,487 · G2 27,668 · G3 11,991 · G4
  35,501 · G5 3,002; audit classes R2 27,668 · R3 30,860 · R4 4,505 · R5
  6,998 · R6 1,793 · R7 6,338 · R9 8,487 (R1/R8/R10/R11 0); sufficiency
  SEC_MORE_MAY_HELP 8,487 · SEC_MORE_UNLIKELY_TO_HELP 4,505 ·
  IDENTITY_DATA_REQUIRED 41,004 · NON_SEC_EVIDENCE_REQUIRED 30,860 ·
  STRUCTURALLY_UNRESOLVABLE 1,793 · UNKNOWN 0. NO_CIK: 1,046 unresolved
  identities hold 27,668 residual months; the 135 declined current-map
  matches: 7 identities, 252 months open before and after (upper bound
  only). 18,953 residual months have a held qualifying filing in reach;
  4,505 of those filings carry no common-stock cover. The 60
  `HELD_COVER_NAMES_SYMBOL_BUT_DID_NOT_JOIN` months were checked by hand:
  the cover rows joined `AMBIGUOUS` at their own date (2025–26 new
  listings), the month-end join `EXACT` — conservative occupancy, not a
  defect.
- A3 utility: 61,628 documents, 5,528,340,425 bytes; 58,540 with
  common-stock cover, 58,050 participating, 1,815 no cover rows, 490 cover
  without benefit, 3,578 zero utility; 2,525 months per 1,000 documents,
  30,225 per GB; 8-K 2.81 months per document and 78,851 per GB against
  10-K 2.43 / 1,139 and 20-F 2.01 / 579. Prefix union: 10% 19,236 (3,121
  per 1,000) · 25% 45,233 · 50% 89,136 · 75% 132,949 · 90% 149,533 · 100%
  155,620; deciles 1–7 2,800–3,100 per 1,000 documents, decile 8 2,550,
  decile 9 1,558, decile 10 988 (6,087 months for 1.21 GB, 5,406 per GB).
- Residual opportunity: 207,938 unheld in-window candidates, 12,320 reach
  8,487 residual months (28,256 edges, 0 future-invalid); forensic plan
  3,640 targets (8-K 2,772 · 10-Q 609 · 10-K 256 · 20-F 3), 2,046,364,597
  estimated bytes, 2.33 months per target, 2,332 per 1,000 documents,
  4,453 per estimated GB, availability UNKNOWN_UNTIL_EXECUTION; 78,162
  months (90.2%) would remain. By year (resolution): 2022 58.8% · 2023
  64.3% · 2024 67.4% · 2025 68.3% · 2026 59.0%. LIVE 71.4% resolved (60,439
  residual), DELISTED 14.7% (26,210 residual); foreign 20-F/40-F filers
  0.35% (25,689 residual, 29.6% of the residual). Concentration: top 10
  identities 0.63%, top 50 3.2%, top 100 6.3%, top 500 31.0%. Recovery
  from the bucket alone (final manifest, post-A3 metadata, PIT package,
  PlanningInputs, A3 record, audit marker/decision/opportunity):
  `PIT_AUDIT_RECOVERY_SUFFICIENT = YES`. Credential scan over 30 files: 0.
- `tests/integration/test_pit_final.py` +8 (14): reproduction and
  reconciliation, the planning matrix summing to every open month, one
  class per residual month from the assembly's reasons (R4, R6, R9 on the
  hand-built world), the opportunity measured by the unchanged planner and
  never authorized, the prefix-union curve and participation utility, two
  audits fingerprinting identically, the package marking the forensic plan
  and mirroring only small files, outcomes refused when they do not
  reproduce their fingerprint.

### B2.16 post-A3 index and final PIT materialization — 2026-09-19

- `evidence build-post-a3-index --expect-manifest-sha a5653f1c…
  --expect-trusted 99774 --expect-baseline-index-sha 3deb46b7…
  --expect-baseline-manifest-sha 13722e02… --expect-baseline-trusted 38146
  --expect-baseline-filings 32413 --expect-baseline-submissions 5733
  --expect-plan-sha 0efa7d03… --expect-plan-rows 61628
  --expect-a3-marker-sha af88cc24… --expect-database-sha … (both)
  --batch-size 250 --prefetch 8`, 14:39–15:39 UTC, one invocation, 0
  resumes: final manifest equal to its newest mirrored snapshot; A3 marker
  local == mirrored; B2.12 index re-proven (digest, metadata, marker,
  integrity ok, five fingerprints); delta 61,628 = 94,041 − 32,413, 0
  missing / 0 extra / 0 duplicate, 45 co-registrant rows; baseline copied
  and re-proven; 61,628 GETs, 5,528,340,425 bytes, 0 mismatches, 0 missing,
  0 storage failures, 0 retries; parsed 61,628, failed 0; forms = the
  plan; the 37.1 GB baseline not read. Rows added: filings +61,628,
  securities +81,478, catalogue +0, catalogue_stats +0. Baseline
  invariance: all five subset fingerprints unchanged. Whole-index
  fingerprints: filings 94,041 `0aebbf30…`, securities 124,479
  `ee19167a…`, cover `b84800d4…`, catalogue `98f66a22…`, catalogue_stats
  `a809c94c…`. Index `e6ce97d6…` (344,674,304 bytes); marker `d91ff18d…`;
  6 control objects mirrored; B2: 61,646 GETs (61,628 evidence), 6 control
  PUT/HEAD/LIST, 0 evidence PUT/DELETE, 0 SEC; both databases unchanged.
- `evidence materialize-pit --expect-index-sha e6ce97d6…
  --expect-manifest-sha a5653f1c… --expect-package-sha 79f644f1…
  --expect-plan-sha 0efa7d03… --expect-universe-sha b8eaed3e… --runs 2`,
  15:40 UTC: pre-A3 coverage `1fc94fc1…` (268,780 months) proven against
  the package manifest; two assemblies (~12 s each) with identical
  fingerprints — month outcomes 242,269 rows `a955a2b7…`, gap transitions
  `7b073478…`, identity summary 6,524 rows `c8b79f26…`, target realization
  61,628 rows `eda53706…`, summary `0a0c55cd…`.
- Outcome: 242,269 open → 155,620 resolved (64.23%), 86,649 still open, 0
  regressed; predicted solvable 164,572 → 155,040 / 9,532; predicted
  unsolvable 77,697 → 580 / 77,117; expected 155,114.479 vs actual 155,620
  (+505.5, ×1.0033; actual / predicted-solvable 0.9456). Transitions:
  G1→RESOLVED 155,040, G1→G1 8,439, G1→G4 1,093, G2→RESOLVED 535, G2→G2
  27,668, G3→RESOLVED 33, G3→G1 48, G3→G4 13, G3→G3 11,991, G4→RESOLVED
  12, G4→G4 34,395, G5→G5 3,002. The 580 predicted-unsolvable resolutions
  follow from the unchanged rule (symbol-joined observations cover an
  identity whose gap class came from its resolved issuer). By year
  (resolved / still open): 2022 32,275 / 22,626 · 2023 34,974 / 19,377 ·
  2024 36,949 / 17,878 · 2025 38,936 / 18,074 · 2026 12,486 / 8,694.
  Identities: 6,524 with open months, 4,028 improved (1,255 fully, 2,773
  partially), 2,496 unchanged; `NO_CIK` 1,048 identities keep 27,668 open
  months. Realized edges 155,620 checked, 0 future-invalid, 0 identity
  violations, 0 identity-state transitions. Targets: 59,813 emitted cover
  rows, 58,540 common-stock cover, 58,030 the source of ≥1 resolved month,
  1,815 no cover rows, 490 cover without benefit; months-as-source sum
  155,620 vs 164,572 predicted potential. Consumers `coverage`, `join`,
  `sample`, `reachability`, `expand` run against the new index. Recovery
  from the bucket alone: final manifest snapshot, baseline marker, B2.12
  metadata/marker, A3 authorization and marker, post-A3 metadata,
  fingerprints and marker, PIT summary, fingerprints and marker, corrected
  package — all read back and equal: `POST_A3_DERIVED_RECOVERY_SUFFICIENT
  = YES`. Credential scan over 32 files: 0. No A4.
- `tests/integration/test_post_a3_index.py` (14) and
  `tests/integration/test_pit_final.py` (6): the baseline re-proven and
  refused on a wrong digest, another manifest or moved rows; the delta
  equal to the plan with missing and extra identities named and refused;
  only the delta read and the baseline file untouched; co-registrant rows
  kept apart; mismatching bytes yielding no rows; resume skipping committed
  artifacts and fingerprinting like one pass; a resume bound to other
  inputs refused; a completed index not rebuilt without `--fresh`; the
  baseline invariance; the measured outcome on a hand-checkable world
  (transitions reconcile, source and participation, re-measured classes,
  determinism, a mismatched universe refused, the package round trip and
  mirror, pre-coverage loaded only when it hashes as recorded).

### B2.15 A3 expansion — 2026-09-19

- `evidence expand-a3 --live --plan …/a3_r1 --expect-plan-sha 0efa7d03…
  --expect-plan-rows 61628 --expect-package-sha 79f644f1…
  --expect-record-sha 2efb042f… --expect-baseline-marker-sha 6c25b3c3…
  --expect-baseline-manifest-sha 13722e02… --expect-baseline-trusted 38146
  --expect-database-sha … (both) --batch-size 1000 --workers 4
  --max-requests 2500 --max-bytes 4294967296`; the first invocation at
  07:59 UTC started from the baseline manifest exactly (`13722e02…`,
  38,146 rows); baseline marker re-proven locally and as mirrored; package
  manifest and authorization record read back from the mirror; 61,628
  targets resolved from the V2 index, 0 unresolved; 62 batches frozen (set
  `2590ff77…`; 61 × 1,000 + 628) and mirrored before the first request.
- Two invocations. Run 1 (07:59–09:57) completed batches 000–027 and
  stopped as verdict B on a `ReadTimeoutError` in the HEAD verifying one
  PUT (`filings/0000101829/000010182924000022/rtx-20240717.htm`), 365
  targets of batch 028 already settled. Fix under §50: bounded retry on the
  bucket side of a target — the next attempt asks whether the object is
  there and is the SEC body (adopt), re-PUTs if absent, stops as D on other
  bytes; two regression tests; `make check` 3,824 passed; plan and database
  digests re-proven before resuming. Run 2 (10:04–13:32): batch 028 resumed
  with 365 cache hits (one HEAD each, 0 SEC requests), the interrupted
  object adopted after the SEC body matched it byte for byte (no re-PUT),
  634 new; batches 029–061 clean; `EXPANSION_HARVEST_COMPLETE` 12:25.
- Accounting: 61,628 planned / 61,628 trusted / 0 failed / 0 pending;
  61,627 new downloads + 1 adopted; 365 cache hits on resume; 0 `FAILED`
  rows in the manifest; 0 rows outside the plan; 45 co-registrant copies
  distinct. Forms planned = trusted: 8-K 59,937 · 10-Q 1,176 · 10-K 380 ·
  20-F 96 · 20-F/A 19 · 40-F 18 · 40-F/A 2. SEC: 61,264 filing GETs, 1
  retry, 0 × 401/403/429, 0 ticker-map, 0 submissions, 0 search. Bytes:
  planned estimate 6,599,925,841, actual 5,528,340,425 (the estimate is a
  per-form catalogue mean). B2 evidence, both runs: PUT 61,628 · HEAD
  123,622 · GET 123,256 (one read-back per PUT + 61,628 terminal) · LIST
  24 · DELETE 0; control PUT 336 / GET 819 / HEAD 336 / LIST 9.
- Milestones 009, 019, 029, 039, 049, 059: objects = trusted (48,146 …
  98,146), 0 orphans, 0 missing, both databases unchanged, manifest
  snapshot mirrored each time. Batches 180–344 s (median 234 s), 4 h 09 m
  of batch time; 5 h 27 m wall including the stop and the terminal pass.
- Terminal: 61,628 A3 artifacts downloaded and hashed — 0 digest
  mismatches, 0 size mismatches, 0 read failures, 5,528,340,425 bytes; the
  baseline's 37.1 GB not downloaded again. Full namespace LIST: 99,774
  objects = 99,774 trusted records, 0 orphan objects, 0 orphan records, 0
  missing, 0 path violations, 0 zero-byte, 0 duplicate URLs; no HEAD per
  object. Final manifest `a5653f1c…` (99,774 records, 42,669,775,288
  logical bytes) snapshotted as `A3_EXPANSION_COMPLETE` with parent
  `13722e02…`; marker `a3_expansion_complete.json` `af88cc24…` mirrored and
  read back; 322 objects under `a3-expansion-r1/`, 0 conflicts.
- Recovery from the bucket alone: baseline, corrected inputs, authoritative
  plan, superseded plan distinguishable (`a3e7df19…` sidecar
  `NOT_AUTHORIZED_FOR_EXPANSION`, `0efa7d03…` record
  `AUTHORIZED_FOR_EXPANSION`), batch plans, completed state, final
  manifest, marker, evidence inventory — `A3_EXPANSION_RECOVERY_SUFFICIENT
  = YES`. Credential scan over 198 files (execution directory, descriptors,
  ledger, manifest, docs, logs): 0. Databases `02a6660b…` / `20a77d24…`
  unchanged before, at every milestone, after, and before the commit.
- `tests/integration/test_a3_expansion.py` (26): authorized-plan
  acceptance; refusals for a superseded directory, a missing record, a
  record without `AUTHORIZED_FOR_EXPANSION`, a plan-digest mismatch, a
  record naming another plan, a record-digest mismatch, moved plan bytes;
  deterministic batch partitioning and never-regenerated membership;
  end-to-end COMPLETE with marker, snapshot and recovery; co-registrant
  identity; no manifest row before the remote body verified; an unrecorded
  object adopted only as the SEC bytes, other bytes stopping as D; a
  transport fault after the PUT recovered by reading back, a persistent one
  stopping as B without a row; verdict C on a persistent 404 naming the
  target; a transient failure retried in-batch; a refusal stopping before
  the next request; resume skipping completed batches and not re-fetching
  trusted targets; a checkpoint bound to other inputs refused; the command
  refusing without `--live` or the digests, executing to COMPLETE, and
  refusing a superseded directory.

### B2.14R2 authoritative Plan A3 — 2026-09-18

- `evidence regenerate-a3 --package …/a3_inputs_r1 --expect-package-sha
  79f644f1… --database stage9k.db --expect-universe-count 8919
  --expect-universe-sha b8eaed3e… --expect-database-sha … (both)
  --expect-manifest-sha 13722e02… --expect-index-sha 3deb46b7… --output
  …/a3_r1 --expect-forensic-rows 61628 --expect-forensic-sha 0efa7d03…
  --authorize --supersedes-package-sha 83680ead… --supersedes-plan-sha
  a3e7df19… --forensic-commit 2a7d3b84… --correction-commit daa1d36a…
  --mirror`, 01:21 UTC 2026-09-19: both database digests unchanged before
  and after; universe re-proven read-only; package re-hashed component by
  component; 0 SEC requests, 0 evidence reads or writes, 0 deletes; planner
  ~7 s for two runs. (A first invocation was refused by the universe
  binding because a shell substitution passed an 8-character digest;
  nothing was written.)
- Frozen systems verified before the run: `plan.py` d6562cab…,
  `reachability.py` cd82fca6…, `join.py` 1a3e00d2…, `coverage.py`
  a0b5816f…, `sample.py` 12abc44e… (all last changed 13d586b), `expand.py`
  fb6a975e… (1b56b86); `WINDOW_START` 2022-01-01, `WINDOW_END` 2026-07-31,
  `CARRY_FORWARD_DAYS` 90, `PLAN_VERSION` v1; `stash@{0}` untouched.
- Plan `0efa7d038e063099dff86ff6e74ea9a2aab61feed62c45f29829511eba9b9507`:
  **61,628 targets**, 4,167 CIKs, 61,583 accessions, 6,599,925,841 bytes
  (6.147 GiB); 8-K 59,937, 10-Q 1,176, 10-K 380, 20-F 96, 20-F/A 19, 40-F
  18, 40-F/A 2; solvable 164,572 reached 164,572, unreached 0; expected
  155,114.479 months, floor 77,697, expected remaining 87,154.5 of 242,269;
  target months 2022-01-31 … 2026-07-31; filing dates 2022-01-03 …
  2026-07-21; validation seven zeros; run 2 identical. Fingerprints:
  unordered `42d92d38…`, identities `3c5344ef…`, per-form `6bcfeb66…`,
  per-CIK `68cc8173…`. Files: csv 22dfdfff…, summary c0a109a8…,
  fingerprints c01c038a…, meta d153da40…, complete 9e5ccc18…,
  authorization 2efb042f….
- `EXACT_FORENSIC_REPRODUCTION`: rows and digest equal to B2.14R run 1.
  Historical: −313 (61,941), `BYTE_DIGEST_COMPARISON_INCONCLUSIVE`,
  count-only comparator, not forced. Versus the superseded `a3e7df19…`:
  61,577 in both, 1,347 only old — exactly its 2021-filed targets — 51 only
  new; 0 targets filed 2021 in the authoritative plan.
- Independent audit over all 61,628 rows: every target a frozen candidate,
  not held, qualifying form, in window, estimate equal to its candidate's;
  every cover key `G1_SOLVABLE`, month-end ≥ filing date and ≤ 90 days
  after; all 164,572 solvable gaps claimed; ranks contiguous; 0 duplicates.
  Edges: 647,373 candidate-gap (0 future-invalid), 182,727 selected-plan
  (0 future-invalid); identity violations 0 (every covered gap's CIK is the
  target's); 45 co-registrant copies, 0 collapsed, 0 accession-only dedup
  errors. Spot audit seeded from the plan digest: 320 samples over top,
  middle, bottom, 8-K, 10-Q, 10-K, 20-F, 40-F, co-registrant, one-month
  marginal and high-marginal strata, 0 failures; plus a 200-row sample
  (seed 20260918) recorded with covered months.
- Mirror: 10 files as 20 objects under `control/…/a3-r1/`, all `MIRRORED`,
  read back equal; 1,302 control objects; superseded `a3/` (`a3e7df19…`,
  `SUPERSEDED.json`) and `a3_inputs/` byte-identical after. Recovery from
  B2 alone (`sqlite3.connect`, the engine factory and the assembly refused):
  27 package files from the newest `a3-inputs-r1/` snapshots → package
  `79f644f1…` → two planner runs → 61,628 / `0efa7d03…` = mirrored plan =
  mirrored authorization record. `A3_PLAN_RECOVERY_SUFFICIENT = YES`; 74
  control GETs, 76 lists, 0 puts, 0 evidence-prefix keys. Credential scan
  over 18 files (plan directory, descriptors, ledger, docs, logs): 0.
- `tests/integration/test_a3_regenerate.py` +12 (34 passed): the forensic
  gate classifies a match, a row mismatch and a digest mismatch without
  changing the plan; the authorization record is deterministic, names the
  chain, and is refused on a mismatch or a non-reproduced run; a superseded
  package is refused before it is read and a superseded plan directory is
  never written over; the mirror prefix keeps `a3-r1/` apart from `a3/` and
  carries the record; the command authorizes only an exact reproduction,
  stops on a mismatch with nothing put, and wants both halves of an
  expectation.

### B2.13R corrected PlanningInputs — 2026-09-18

- `evidence build-a3-inputs --database stage9k.db --expect-universe-sha
  b8eaed3e… --expect-database-sha … --expect-manifest-sha 13722e02…
  --expect-index-sha 3deb46b7… --supersedes-package-sha 83680ead…
  --supersedes-plan-sha a3e7df19… --forensic-commit 2a7d3b84…`, 22:45 UTC:
  both database digests unchanged before and after; every binding
  re-proven (index and five fingerprints, mirrored derived-index marker,
  resolution companion, universe); 0 SEC requests, 0 evidence reads or
  writes; assembly 11.3 s.
- Window evidence: `plan.WINDOW_START` 2022-01-01 / `WINDOW_END` 2026-07-31
  (byte-identical since 13d586b); no catalogue filing dated 2022-01-01 or
  2021-12-31; 348 dated 2026-07-31, which `supported_months` credits with
  the 2026-07-31 month-end; inclusive both ends.
- Corrected package `79f644f1f8af85422b68911ef5427e6150886294a8ae8b4da57a34e4c9a7622e`
  (`B2.13R/1`, `HISTORICAL_9L_V1`): 291,623 window filings, 22,057 held in
  window, **269,566 candidates** (9L.1: 269,521), 227,642 with a cover key,
  647,373 cover keys over exactly the **164,572** solvable gaps; open
  242,269 = G1 164,572 + G2 28,203 + G3 12,085 + G4 34,407 + G5 3,002;
  floor 77,697; held 32,413. Removed from the superseded package: 10,206
  candidates, all 2021 (Nov 7,133, Dec 3,073; 8-K 7,174, 10-Q 2,844, 10-K
  181, foreign 7) over 3,503 issuers, 0 added, common rows identical;
  1,503 months G1 → G4 (Jan 2022 1,319, Feb 2022 184; 1,319 identities).
  Component digests: coverage 1fc94fc1…, reachability 0ff895b3…, gaps
  2037534d…, candidates 56719b7d…, held 4ac8220f…, yield 4bc5064c…, bytes
  84c270fa…, conflicts 49350662… — reachability, held, yield, bytes and
  conflicts unchanged from the superseded package. 647,373 candidate-gap
  edges, 0 future-invalid, the same 3 SEC-index acceptance anomalies.
- Determinism: two in-process assemblies and a second invocation into
  another directory — every component and the package digest identical;
  manifest and marker differ only by `supersedes` and `timings`.
- Mirror: 27 files as 54 objects under `control/…/a3-inputs-r1/`, all
  `MIRRORED`, every one read back equal; the superseded package's 50
  objects intact and still recoverable; `SUPERSEDED.json` mirrored beside
  the old package and plan; mirror 1,282 objects; credential audit 0.
- `tests/integration/test_a3_inputs.py` +12 (49 passed): the window
  inclusive at both ends, the constants, November and December 2021
  filings never candidates while 2022-01-01 and 2026-07-31 are, January
  2022 reached only by an in-window filing, the summary accounting for
  every out-of-window row, a package under another window definition
  refused, the supersession record and sidecars written without touching
  the old artifacts, an existing sidecar never overwritten.

### B2.14R forensic A3 difference review — 2026-09-18

- Read-only over the frozen package (`83680ead…`), the frozen plan
  (`a3e7df19…`) and the V2 index; both database digests unchanged; plan and
  package byte-identical after; 0 SEC requests, 0 evidence reads or writes;
  16 forensic files mirrored as 32 snapshots under `control/…/a3/forensics/`
  and read back (96 GET / 32 PUT / 32 HEAD / 34 LIST, all control).
- Recovery: `V1_ROWS_NOT_RECOVERABLE` — no plan row or batch list in git
  history, docs, fixtures or the control root; the stash is B2.9R rescue
  tooling. Timeline: universe built 2026-09-04 15:56 UTC (proven; stage9k
  unwritten since), V1 acceptance ingest 2026-08-30 (proven), V1 baseline /
  batches / plan ≤ 2026-09-07 (inferred), V2 catalogues 2026-09-16/17
  (proven), V2 plan 2026-09-18 (proven).
- Identity: plan/reachability/join/coverage/sample byte-identical to
  `13d586b`; expand storage-only; inputs new. Configuration proven the same
  on strategy, score, limit, minimum marginal, stamp, forms, reachability and
  order key; **candidate window proven different** (historical strict window
  reproduces the 9L.1 inventory to within 45 candidates).
- Ablation: run 0 62,924 / `a3e7df19…`; run 1 (V1 window only) 269,566
  candidates, 61,628 targets, 6,599,925,841 bytes, packing 2.670, potential
  164,572 (1,503 Jan/Feb-2022 months unreachable), expected 155,114.5,
  `0efa7d038e063099dff86ff6e74ea9a2aab61feed62c45f29829511eba9b9507`; the
  difference is the 1,347 frozen targets filed in 2021 less 51 replacements,
  all within the 3,503 issuers holding 2021 candidates. Runs 2/3 not
  runnable (bound ≤ 800). No historical-count hunting.
- Bounded, unlisted: V1's batch 1 unheld in V2 (≤ 800); the 135 declined
  mappings (≤ 252 months of V1-only demand); V1's `NO_CIK` excess (36,661 vs
  28,203, identities unrecoverable); baseline −171 / window held 22,057 vs
  23,345; V2 yields (8-K 0.944 vs 0.922) raising the expectation ratio
  0.9425 vs 0.9204. Residual after run 1: −313 (−0.5%).
- Ties: faithful replay reproduces the frozen plan; 58,707 of 62,924 steps
  tied on all four scored components (max width 323), decided by
  `(cik, accession)`. Temporal: 657,826 edges, 0 future-invalid, 3 SEC-index
  acceptance anomalies (none selected). Co-registrants: 24 accessions, 49
  rows, all disjoint identity sets. Independent validation all zero; 28 spot
  samples, 0 failures.
- `tests/integration/test_a3_regenerate.py` +2: same-day filings of
  different issuers tie and order by identity regardless of input order; a
  candidate added for one issuer changes only that issuer's selection and
  the ordered digest, never the other issuer's targets or the per-form
  composition. **22 passed.**
- Verdict **C**; proposed fix (unapplied): decide the candidate window —
  restrict `inputs.py` to the 9L window, or version the extended window as
  policy — then rerun B2.13 and B2.14.

### B2.14 Plan A3 regenerated — 2026-09-18

- `evidence regenerate-a3 --expect-package-sha 83680ead… --database stage9k.db
  --expect-universe-sha b8eaed3e… --expect-database-sha … --expect-manifest-sha
  13722e02… --expect-index-sha 3deb46b7…`, 19:59–20:01 UTC: both database
  digests unchanged before and after; the package re-hashed component by
  component and bound to the live manifest, index, five fingerprints,
  resolution and universe; 0 SEC requests, 0 evidence reads or writes.
- Planner: `plan_coverage_first`, no limit, min marginal 1, plan stamp `v1`,
  written by `write_plan`; algorithm files digested into the metadata and
  byte-unchanged (`plan.py` `d6562cab…`, `reachability.py` `cd82fca6…`,
  `join.py` `1a3e00d2…`, `coverage.py` `a0b5816f…`, `sample.py` `12abc44e…`,
  `expand.py` `fb6a975e…`).
- Result: **62,924 targets**, SHA-256
  `a3e7df1929d3160158334de7c163b09bf5e644f6a7c353a6e377b532871dcf7b`; 4,174
  CIKs, 62,875 accessions (49 co-registrant copies); 6,807,725,520 bytes
  (6.34 GiB); safe potential 166,075 = every solvable gap, 4,213 of 4,213
  identities reached; expected 156,531.554 (0.9425 of potential); floor
  76,194; expected remaining 85,737.4 of 242,269 open; forms 8-K 61,160,
  10-Q 1,249, 10-K 378, 20-F 97, 20-F/A 20, 40-F 18, 40-F/A 2; months per
  target 2.639. Validation: 0 outside the pool, 0 held, 0 duplicates, 0
  invalid forms, 0 without cover, 0 wrong estimates. Run 2: identical rows,
  bytes, digest, metrics.
- Historical: 61,941 / `53e3f0ca…`, ~6.6 GiB, 165,553 potential, 152,381
  expected (0.9204), floor 76,029, 2.673 months per target. Row-count match
  **no** (+983, +1.6%); digest match **no**;
  `BYTE_DIGEST_COMPARISON_INCONCLUSIVE` for the bytes, a real difference for
  the selection. Decomposition from the frozen package alone: +190 targets
  from the 522 extra solvable months at historical packing, +793 from the
  lower packing — which is a property of the V2 candidate pool (279,772 vs
  ~269,5xx; catalogues two weeks fresher; the 800 and 1,008 V1 validation
  filings unheld and eligible; 22,108 held-excluded vs V1's 33,384). The 135
  declined mappings only remove demand (252 `G2_NO_CIK` months); the
  one-month boundary is shared code and contributes nothing; V2's higher
  yields (8-K 0.944 vs 0.922) raise the expectation, not the count. No row
  cause is provable: the historical rows are lost.
- Forensic fingerprints: ordered `a3e7df19…`; unordered targets
  `0dc99399853198cdd1259a3f6964acb1b586d690bc94d4e5c05743ac344b2f6a`;
  ordered identities `ae6c65f41d5e9a750a1748d98d4c4a7b0ac78f52dad0fd433ff99da3a0f01708`;
  per-form `8a5daade94f8e1e1f7d5dda1885401355d246d4edb51cfc30315b3cb56ab9da4`;
  per-CIK `f76135c794df72db484dd6fbc2c1dd814b37aa014e077a73da0d1291e768baec`.
- Mirror: 9 files as 18 objects under `control/…/a3/`, all `MIRRORED`,
  every one read back equal; 54 GET / 18 PUT / 18 HEAD / 18 LIST, all
  control; credential audit 0.
- `tests/integration/test_a3_regenerate.py`: **20 passed**, offline — the
  planner running with the database, network and assembly disabled, two
  runs byte-identical, a mutated component refused before the planner, a
  moved source refused, the inconclusive-not-mismatch comparison, a target
  outside the pool / already held / duplicated / uncovered / of a foreign
  form or estimate each failing validation, the real plan passing,
  regeneration stopping on an invalid plan, the writer's bytes for one
  target pinned with their digest, the writer and the in-memory bytes
  agreeing, fingerprints separating order from membership, every plan file
  mirrored, and the command planning and mirroring without evidence
  access, refusing a changed component and a moved index.

### B2.13 A3 planning inputs — 2026-09-18

- `evidence build-a3-inputs --database stage9k.db --expect-universe-count 8919
  --expect-universe-sha b8eaed3e… --expect-database-sha … --expect-manifest-sha
  13722e02… --expect-index-sha 3deb46b7…`, 18:39–18:42 UTC: both database
  digests verified before and after (unchanged), the derived-index marker
  verified against the manifest, the index, its recomputed fingerprints and
  its mirrored copy, the frozen resolution against its companion; 0 SEC
  requests, 0 evidence bodies read, 0 evidence writes. Assembly 12.8 s.
- Two assemblies in one process and a second invocation into another
  directory: every component byte-identical, package SHA-256
  `83680eadaa06317c10a0a26e68f5547a022207dc1bc1e5c1283a8427b5db3ce5` three
  times; only the manifest's `timings` differ. The package round-trips to
  equal `PlanningInputs`.
- Coverage: 268,780 months, 6,652 identities, 55 months; `DECIDED` 10,317,
  `COVERED` 16,194 (12,345 under the stricter classification rule),
  `OPEN` 242,269 = `G1_SOLVABLE` 166,075 + `G2_NO_CIK` 28,203 +
  `G3_AMBIGUOUS` 12,085 (5,878 symbol not attaching, 5,807 not credited
  under `SAFE_COMMON_CLASS`, 400 `IDENTITY_AMBIGUOUS`) + `G4_NO_CANDIDATE`
  32,904 + `G5_OTHER` 3,002 (1,793 no listing observation, 1,209
  `MULTI_CLASS_UNASSIGNED`). Raw `UNKNOWN` floor 76,194 against the
  historical 76,029; solvable 166,075 against the historical 165,553 safe
  potential months. Identity states at the month-end: 260,723 `EXACT`,
  6,260 `AMBIGUOUS`, 1,758 `OTHER_OCCUPANT` (78 live identities), 39
  `UNRESOLVED`.
- Reachability: 42,204 symbol-bearing cover rows, 31,464 `EXACT` / 819
  `AMBIGUOUS` / 9,921 `UNRESOLVED`; 28,849 common-stock rows joined; 6,048
  tracked identities on 5,733 issuers: `SAFE_SINGLE` 5,471,
  `SAFE_COMMON_CLASS` 201, `MULTI_CLASS_UNASSIGNED` 21, `IDENTITY_AMBIGUOUS`
  40; 5,720 credited, 0 under `MULTI_CLASS_UNASSIGNED` or
  `IDENTITY_AMBIGUOUS`; 222 multi-identity issuers with 537 identities, 249
  credited; 142 sole identities whose held rows are all non-common remain
  `SAFE_SINGLE` by the frozen rule.
- Candidates: 716,290 catalogue rows → 301,880 that can reach a window
  month-end → 279,772 after 22,108 held; 4,616 issuers; 235,630 carry a
  cover key; 657,826 cover keys over exactly the 166,075 solvable gaps;
  279,448 distinct accessions (324 co-registrant copies kept apart). Forms:
  8-K 210,223, 10-Q 50,762, 10-K 18,504, 20-F 198, 20-F/A 40, 40-F 36,
  40-F/A 9. Yield: 8-K 0.944, 10-Q 0.931, 10-K 0.883, 40-F 0.718, 20-F/A
  0.653, 40-F/A 0.619, 20-F 0.588; mean bytes 8-K 41,625, 10-Q 2,121,395,
  10-K 2,736,244, 20-F 5,460,753.
- The 135 declined map matches: 264 universe rows, 252 open, all
  `G2_NO_CIK`. All 2,871 unresolved identities: 29,674 rows, 28,203 open —
  every `G2_NO_CIK` gap.
- Conflicts (diagnostic; the frozen table untouched): of 54,998 rows with a
  `class_source_accession`, 5,333 are held in V2 and join to the same
  identity with the same acceptance date (0 timestamp disagreements), 785
  held but not joining, 44,625 catalogued but not held by the
  six-per-security baseline, 4,255 absent from the V2 catalogue.
- Mirror: 25 files as 50 objects, all `MIRRORED`, every one read back equal
  to the local file and its descriptor; mirror 1,174 objects; credential
  audit over every log, package file and control file: 0.
- `tests/integration/test_a3_inputs.py`: **37 passed**, offline — the PIT
  cutoff forward and never backward, acceptance date over filing date, a
  filing after the cutoff, a month without a listing observation, a decided
  month, the dashed accession form, `SAFE_SINGLE` and solvable versus
  no-candidate gaps, two genuine common classes both credited, an
  unattributable issuer crediting nothing, the baby bond excluded while the
  common line is credited, a symbol-less row never an observation, the
  foreign filer, no-CIK gaps, ticker reuse covering each occupant from its
  own filings only, the held set on `(cik, accession)` with co-registrants
  apart, candidates excluding held and reaching only open solvable
  month-ends, an out-of-window filing, measured yield and sizes, the status
  partition, round trip, byte-identical double assembly, every source
  binding and fingerprint enforced, tampered components and manifests
  refused, the resolution companion, mirroring every file, and the command
  freezing without planning, refusing a marker that does not describe the
  index, and refusing a differing mirrored marker.

### B2.12 derived index rebuild — 2026-09-18

- `evidence build-index --expect-manifest-sha 13722e02… --expect-trusted 38146
  --expect-database-sha …` in four invocations, 12:19 to 14:47 UTC, every
  one starting from `BASELINE_COMPLETE` verified locally and against its
  mirrored copy, both database digests verified before and after
  (`02a6660b…`, `20a77d24…`, unchanged), 0 SEC requests, 0 evidence `PUT`
  or `DELETE`, credential audit over every log, control file and metadata
  file: 0. Invocations 1 and 2 were `--stop-after 40` (sequential) and
  `--stop-after 300 --prefetch 8` smoke runs; invocation 3 (`--prefetch 12`)
  saturated boto3's default pool of 10 and was killed with `SIGTERM` in its
  seventh batch — SQLite rolled the hot journal back, the ledger stood at
  1,840 `COMMITTED` and `integrity_check` was ok; invocation 4
  (`--prefetch 8`) resumed at 1,840 without a download for any of them and
  ran to completion: 36,306 processed, 36,842,433,576 bytes, 8,468.9 s
  (2 h 21 min), 36,334 `GetObject` calls — 36,306 bodies, 1 marker verify,
  18 mirror reads, 9 reads retried after transport faults — 6 `PUT`, 6
  `HEAD`, 6 `LIST` (all control mirror), 0 discarded bodies. Across all
  four: **38,146 bodies read once, plus at most 262 discarded by the kill**
  — one corpus pass; 0 byte mismatches, 0 digest mismatches, 0 missing.
- Result: 5,733 catalogues parsed (50 name no qualifying filing; 2,052
  reference 2,605 older history files never held; 21 accessions stamped
  differently by two co-registrant catalogues, the later kept); 32,413
  filings parsed, 0 failures, all 32,413 dated `ACCEPTED` from their own
  catalogue's stamp (1,358 accepted after the close and so dated the
  business day before their filing date — the existing 9L.1 rule; 0 dated
  after); 43,001 registered-class rows — 29,999 `COMMON_STOCK`, 4,775
  `NOTE_OR_DEBT`, 2,241 `WARRANT`, 2,207 `UNIT`, 1,394 `PREFERRED_STOCK`,
  1,000 `DEPOSITARY_SHARES`, 778 `UNKNOWN`, 340 `RIGHT`, 267 `ADR_OR_ADS`;
  39,623 `TITLE_ONLY`, 2,526 `TITLE_AND_MEMBER`, 633 `ABSENT`, 145
  `CONFLICTING`, 74 `MEMBER_ONLY`; 42,849 with a `Security12bTitle`, 42,207
  a `TradingSymbol`, 42,691 a `SecurityExchangeName` (NASDAQ 25,235, NYSE
  16,339, NYSE American 1,037, `UNKNOWN` 314, `OTHER` 76); 17 co-registrant
  accessions kept as 29 surplus `(cik, accession)` rows with one distinct
  digest; symbol provenance 30,136 `CORROBORATED` / 2,277 `MISSING`. Form
  counts equal the manifest's exactly: 8-K 20,058, 10-Q 7,334, 20-F 3,040,
  10-K 945, 40-F 627, 20-F/A 346, 40-F/A 63.
- Validation: `integrity_check` ok; 0 filings without identity, 0
  duplicate `(cik, accession)`, 0 security rows without their filing, 0
  malformed accessions; table counts equal the ledger's; the fingerprints
  recomputed from the promoted file equal the mirrored fingerprint file;
  `span_statistics`, `form_value`, `class_breakdown`,
  `catalogue_opportunity` and `held_keys` open the V2 index unchanged
  (`catalogue_opportunity` reports 32,430 downloaded of 716,290 qualifying
  because it joins on accession alone — the 17 co-registrant accessions
  again). Plan A3 was **not** run.
- **V1, for context only** (the lost index): 32,584 filings, 43,163
  security rows, 718,265 catalogue rows, ~229 MB. V2 holds 171 fewer
  filings, 162 fewer security rows and 1,975 fewer catalogue rows, in line
  with the 38 fewer catalogues and 171 fewer documents the V2 baseline
  holds by resolution policy; the 234 MB file carries the 38,146-row
  ledger. No count is an acceptance criterion.
- `tests/integration/test_derived_index.py`: **34 passed**, offline — one
  read per artifact and never a digest, the existing extraction semantics,
  co-registrant rows kept apart, missing cover fields kept missing, the
  frozen input order, digest and size mismatches recording nothing, a
  missing object, resume after an integrity stop, resume without
  re-reading, resumed and uninterrupted fingerprints equal, a different
  manifest refused, a tampered ledger refused, `--fresh`, a completed build
  not repeated, prefetch reading once and fingerprinting identically, a
  stop under prefetch, a failure under prefetch discarding at most the
  depth, a storage fault leaving a resumable build and an untouched
  manifest, malformed catalogues classified, a parser exception ledgered,
  two builds fingerprinting identically, the marker and files agreeing with
  the report, a foreign kind accounted without a read, the consumers,
  snapshot mirroring with the index never mirrored, a second mirror as
  hits, and the command's binding refusals and resume.

### B2.11 full baseline rebuild — 2026-09-16 to 2026-09-18

- `evidence rebuild-baseline --live --expect-universe-sha b8eaed3e… --expect-database-sha …`
  three times against `stage9k.db` read-only; universe digest and both
  database digests verified before each run and at every milestone,
  unchanged throughout (`02a6660b…`, `20a77d24…`).
- Chunks 001–035 `COMPLETE` in order, each with its plans frozen and
  mirrored, its targets derived twice and byte-identical, every new artifact
  read back before its row, every target downloaded and hashed in the
  replay, its prefixes reconciled, its mirror proven (`recovery=proven`).
  Invocation 1 stopped at chunk 003 on a B2 `PUT` timeout (correct; the
  object never landed); invocation 2 resumed 003 as 167 + 669 `CACHE_HIT`
  and ran to 035, then stopped in the terminal walk on a broken stream;
  invocation 3 treated all 35 chunks as no-ops and passed the terminal gate.
- Milestones 005–035: objects = trusted at every one (6,667 … 38,146), 0
  orphans, 0 missing, 0 path violations; mirror audit clean; databases
  unchanged.
- Terminal gate 2026-09-18 00:09 UTC: **38,146 / 38,146 verified from
  downloaded bytes**, 0 hash mismatches, 0 byte mismatches, 0 read failures
  (one read retried); reconciliation 38,146 objects = 38,146 trusted,
  orphans 0/0, path violations 0, zero-byte 0, duplicates 0; final snapshot
  `manifests/evidence-13722e021e2d4e03.jsonl` mirrored; bucket-only recovery
  proof sufficient; `baseline_complete.json` mirrored (`6c25b3c3…`).
- Authoritative V2 baseline: 8,919 identities; 6,048 resolved; 2,871
  unresolved; 5,733 unique filers; 5,733 trusted submissions; 32,413 trusted
  filings; 38,146 trusted; 37,141,434,863 logical bytes; 38,146 manifest
  records; manifest SHA-256
  `13722e021e2d4e03d13784c5c829320bad5b7dc74055378057bf8804d356086b`.
- Network: SEC 37,152 attempts / 36,301 documents / 36,255,728,600 bytes,
  72 `503` and 18 no-response attempts all retried within policy, 0 `403`,
  0 `429`, ticker map never refetched; B2 38,110 PUT (37,045 evidence, 1,064
  control), 140,946 GET, 428,812 HEAD, 34,034 LIST, 3 transport faults, 0
  conflicts, 0 deletes; mirror 1,064 `MIRRORED` / 547 `MIRROR_HIT` / 0
  `CONFLICT`. Credential audit over every log and control file: 0.
- `tests/integration/test_baseline_rebuild.py`: **36 passed**, offline —
  progression and closure over a four-chunk universe, shared filers as one
  catalogue and `CACHE_HIT`, the partial last chunk, no ticker-map refetch,
  unresolved identities without targets, range and order refusals, no-op
  reruns, tampered frozen files, resume mid-chunk, a failing terminal gate
  leaving no marker, a `403` before the next request, `REMOTE_ORPHAN` and
  `REMOTE_UNTRUSTED` never overwritten, a stray object stopping the next
  chunk without a body fetch, clean milestones at listing cost, the
  milestone hook, frozen resolution and ticker-map drift, the per-chunk
  budget, transient and persistent transport faults, integrity never
  retried, the terminal walk's read retry, the mirror's per-chunk contents,
  the deterministic marker as a `MIRROR_HIT`, bucket-only recovery, mirror
  conflicts, structural reconciliation, and the command. Two harvest tests
  added (`refetch_invalid=False`, a shared replay state); two B2.10
  assertions updated to the scoped stop semantics (`REMOTE_ORPHAN` before
  any overwrite; the renamed replay step).

### B2.10 rebuild canary, chunk 000 — 2026-09-16

- `evidence rebuild-canary --live --expect-universe-sha b8eaed3e…` against
  `stage9k.db` read-only; both database digests unchanged before and after
  (`02a6660b…`, `20a77d24…`).
- Preflight: 0 trusted, 0 completed, every artifact prefix empty. Offline
  plans verified against their digests and mirrored, 6 files read back.
- Ticker map fetched once, 797,759 bytes, `82cd5fd9ccffda81…`, preserved
  raw with sidecar. Resolution of all 8,919 identities, deterministic:
  402 `STORED`, 5,646 `SEC_TICKER_MAP`, 2,871 `UNRESOLVED` (135 delisted
  symbols in the map declined); `filer_resolution_v1.jsonl`
  `161429f58606e0c069eb23ca79fc8b9a6aafe65a4b39238f7d8f32a315132c6b`.
- Chunk 000 frozen: positions 0–249, `identities.jsonl`
  `14f41c86200248179992eefb3b5d330e82792f1f0f040d79c1fb8519b22376d2`;
  180 resolved identities over 165 filers. Filing targets: 936 over 936
  `(CIK, accession)` pairs, cap 6, newest first, derived twice identical,
  `0c67536b6cc3c312466dd04659eb600e233bfd32a874f5f6df5890dbae3b2497`.
- Harvest: 165 catalogues (17,319,117 bytes, 185 s) then 936 documents
  (868,387,146 bytes, 1,315 s), every one read back from the bucket and
  hashed before its manifest row. Manifest 1,101 records, 1,101 trusted,
  0 `FAILED`. SEC 1,106 metered attempts (one `503`, two body timeouts on
  20-Fs, one no-response — all retried within policy; no `403`, no `429`).
  Bucket 1,136 PUT / 1,136 HEAD / 2,258 GET / 3 LIST / 0 delete. Mirror
  35 `MIRRORED`, 2 `MIRROR_HIT`, 0 `CONFLICT`. Credential audit: 0.
- First invocation's convergence replay stopped at 754 of 1,101
  `CACHE_HIT` by the bucket's `AccessDenied` — Backblaze's daily download /
  Class B cap, confirmed afterwards by one HEAD and one GET. Checkpoint
  `PASS2_DONE`, stop recorded in its notes.
- Resume (caps removed), same command, 02:46:49–03:14:07 UTC: preflight
  reconciliation clean over 1,101 trusted; mirror 26 `MIRROR_HIT` / 0
  `CONFLICT`; ticker map reused from disk; resolution, identities and
  targets reproduced byte-identical; passes 165 + 936 `CACHE_HIT`;
  convergence **1,101 / 1,101 `CACHE_HIT`, 0 fetches, 0 bytes, 0 PUTs**;
  full reconciliation trusted 1,101 = objects 1,101, orphans 0/0,
  mismatches 0/0, path violations 0, zero-byte 0, duplicates 0,
  885,706,263 logical bytes, every artifact downloaded and hashed;
  checkpoint `COMPLETE` (`accounting.trusted 1101`, `verification.full_corpus
  1101`); final manifest snapshot `4328d3b463ab08875c4dd54ab9519323cc1a33e3cd15f1e7ff3ff7c39fcac886`
  mirrored; recovery proven by the run (54 mirrored objects) and by an
  independent bucket-only script that hashed all 1,101 evidence objects
  against the mirrored manifest. **SEC requests 0; evidence PUTs 0**;
  evidence HEAD 6,606, evidence GET 3,633, control PUT 19, control GET 72,
  LIST 15. Credential audit: 0. Database digests unchanged.
- `tests/integration/test_rebuild_canary.py`: **45 passed**, offline —
  resolution rules and determinism; the pre-manifest read-back (tampered
  bytes → `EvidenceWriteError`, 0 manifest rows, object left in place; the
  HEAD check alone would have trusted them); mirror new/hit/conflict/
  read-back mismatch, unsafe keys and artifact prefixes refused before any
  request, digest-addressed snapshots never overwritten, mirrored files
  invisible to reconciliation; one chunk end to end with recovery proven,
  checkpoint digests, PUT-before-GET ordering, byte-identical frozen files
  across two generations, replay with 0 fetches / 0 PUTs, forms and
  ordering; stops on a non-empty bucket, a V1 checkpoint, a `403` before
  the next request, a persistent failure after two passes, a tampered
  object, a mirror conflict, the request budget; resume after submissions,
  mid-filings, an unrecorded object left as an orphan, a completed rerun
  all hits, first-run spend kept on the checkpoint, no stage demotion in
  mirrored snapshots; V2 zero start; the command refusing without `--live`, on a
  local backend and for any other chunk, and completing a seeded chunk
  with `evidence status` reporting it.

### B2.9X clean-room reconstruction — 2026-09-16

- `stage9k.db` bound read-only: 8,919 identities (5,769 LIVE, 3,150 DELISTED),
  universe digest `b8eaed3e737fb73a3f7288f0f96aa2bed2bef072f977d1abf67439e2af75f8b5`
  — exact match to the frozen value; 402 stored CIKs, 8,517 without;
  `filing_acceptance` 5,889,860 rows; `research_universe_months` 268,780 rows;
  `ftd_source_files` 433. Database digest unchanged after every read.
- `evidence generation-plan` run live (read-only): identity plan
  `3eac451154403f2705cce1679ad1455bdc6df39966f9feba8deebc05eb1c2934`,
  offline targets `c186934b4b638e8ff983f5ed8a67e428ada7d2af2a8c45e109fc84c024014f02`,
  both identical on a second run; production B2 `{'filings': 0,
  'submissions': 0, 'fulltext': 0}`; V2 progress 0 / 0; A3
  `MISSING_PREREQUISITE`.
- Repository archaeology: `PlanningInputs` is constructed nowhere in `src/`;
  the A3 assembly ran from uncommitted working files. `plan.py` holds the
  algorithm; `sample.py`, `expand.py`, `harvest.py`, `sweep.py` hold the
  baseline policy (universe order, chunks of 250, `resolve_ciks` STORED →
  EDGAR → UNRESOLVED, `FILING_FORMS`, cap 6 per security, most recent first).
- `tests/integration/test_generation_plan.py`: **15 passed**, offline —
  universe order and chunking, stored-CIK targets, `EDGAR_REQUIRED` never
  guessed, byte-identical plans and digests, universe digest enforced, zero
  start state, V1 checkpoints over an empty manifest reported inconsistent, a
  supported checkpoint counted, A3 missing with inputs named, an empty bucket
  costing three LISTs, bucket objects reported not adopted, the command with
  no SEC client and no write, a non-empty prefix stopping it, the wrong
  database refused.

### B2.9 migration planner — 2026-09-15

- `tests/integration/test_migration_plan.py`: **33 passed**, offline. Manifest
  replay (failed→downloaded→cache hit→second URL folds to one key; a trailing
  failure untrusts; conflicting bytes across URLs is `CONFLICT`; unsafe keys
  and unreadable lines are counted and excluded); manifest-only plan; metadata
  and verify probes with present, missing, size- and SHA-mismatched copies; an
  unmounted source asked once; a source failing mid-probe keeping two answers
  and marking the rest `UNAVAILABLE`; the source never walked; an empty bucket
  costing three LISTs; remote verified by GET, wrong size without a GET, wrong
  bytes despite a lying metadata claim, orphan, canary object ignored, listing
  failure stopping the planner, no PUT/DELETE ever; precedence and the
  exactly-once invariant; deterministic bytes, digest, batches and summary;
  A3 partition invariants; the command blocked on a missing manifest,
  writing a plan on a present one, and blocking on remote orphans.
- Live, read-only, 2026-09-15: `evidence migration-plan` → **BLOCKED —
  AUTHORITATIVE MANIFEST UNAVAILABLE**; remote `{'filings': 0,
  'submissions': 0, 'fulltext': 0}`; nothing written.

### B2.8 canary harness — 2026-09-15

- `tests/unit/test_b2_canary.py`: **38 passed**, offline. A full run over the
  fake passes every step and cleanup empties the prefix; every object written
  sits under `_canary/<run-id>/` and none under an artifact prefix; a
  truncated upload, a lying GET body, a dead endpoint at preflight and a
  non-empty prefix each stop the run before or at the right step and still
  clean up; cleanup refuses the bucket, the canary root, every artifact prefix
  and multi-run prefixes; `run_live_canary` through `open_evidence_layout`
  never opens the production control root; the command refuses without
  `--confirm` and reports without secrets with it.
- `tests/unit/test_storage_b2.py`: +3 for `NoSuchBucket` (**108 passed**).
- **Live, 2026-09-15**: `evidence b2-canary --confirm` → run
  `b28-20260915T233118Z-5a5c6689`, `CANARY PASSED`, 19 steps ok, 7 objects /
  2,098,604 bytes, 7 PUT / 83 HEAD / 32 GET / 15 LIST / 1 batch DELETE, zero
  SDK retries, 12 s wall; `pytest tests/live --live-b2` → run
  `b28-20260915T233201Z-a9b49b3b`, 1 passed, identical accounting. Output
  searched for both credentials: absent. Bucket afterwards: `filings/`,
  `submissions/`, `fulltext/`, `_canary/` all empty; production control root
  `.evidence/` holds no manifest.

### B2.7 backend implementation — 2026-09-15

- Shared contract: `test_storage_contract.py` now runs 57 cases × 3 backends
  (**171 passed**); `B2EvidenceStorage` over `FakeS3Client` passes every one
  the local and memory backends pass, unchanged.
- `tests/unit/test_storage_b2.py`: **105 passed**, offline — exists/stat/
  read/digest/write/list matrices (404 vs 403 vs 5xx vs 429 vs connection and
  read timeouts vs short body), unsafe keys issuing zero calls for all six
  operations, PUT+HEAD verification with length, metadata and vanished-object
  mismatches, digest from bytes over a lying metadata claim, pagination with
  out-of-order and repeating pages, a sloppy server's segment leak filtered,
  no delete ever issued, secrets absent from `repr`/exceptions/causes/logs,
  and `botocore.stub.Stubber` wire checks of the PUT parameters and the real
  SDK's 404/403/NoSuchKey.
- `test_storage_neutral.py`: the reconciliation matrix now runs over B2 too
  (**105 passed** across memory/local/b2) plus the provenance gate against
  B2: failed upload, truncated upload and failed verification HEAD each leave
  the manifest empty; replay is a cache hit by GET+SHA; a tampered object is
  refetched; extraction reads bucket bytes.
- 0 network requests in any test; no credential other than sentinels.

### B2.6 configuration boundary — 2026-09-15

- `tests/integration/test_storage_configuration.py`: **51 passed**, offline.
  Selector defaulted/explicit/invalid; credentials alone do not select `b2`; a
  local root does not pull a `b2` run back; each missing `B2_*` setting named,
  all at once; six malformed endpoints and six non-bucket-names refused; an
  unfamiliar https host accepted with no derived region; incomplete `b2` with a
  local root available raises and touches neither root; a complete `b2`
  configuration opens the control root and stops at the B2.7 seam; `b2` with
  no or a missing control root raises and creates nothing; a known application
  key absent from `repr`, `str`, `describe()`, four error messages, captured
  logs and every status output; status for local separated, legacy colocated,
  `b2` configured, `b2` missing a setting, `b2` without a control root, and a
  misconfigured `b2` harvest stopping with a `StorageConfigurationError`.
- Every pre-B2.6 evidence suite passed unmodified.

### B2.5 control-plane separation — 2026-09-15

- `tests/integration/test_control_plane.py`: **35 passed**. Artifact root
  removed with the control root intact — manifest entry, checkpoint (stage
  `COMPLETE`), target manifest and fingerprint all read back; every artifact
  operation raises `StorageUnavailableError` and the fake SEC client records no
  further request. Control root removed with the artifacts intact —
  `Manifest.entries`, `load_checkpoint`, `harvest_artifacts` and
  `open_control_root` all raise `ControlPlaneUnavailableError`, no directory is
  recreated, no request is spent.
- The manifest line written under the separated layout equals the line the
  colocated layout writes, `recorded_at` aside; neither root's path appears in
  it.
- A memory backend passes `preflight_corpus` with a one-petabyte reserve: no
  universal disk-capacity requirement remains.
- Every pre-B2.5 evidence suite passed unmodified: 613 tests across the storage,
  harvest, offline, expand, sweep, CLI, targets, plan, dedup, reachability,
  budget, manifest-scale and pagination files.

### B2.4 refactor verification — 2026-09-14

- Final focused storage/evidence pass: **417 passed**, exit 0. Suites:
  `test_storage_contract`, `test_storage_local`, `test_storage_neutral`,
  `test_storage_equivalence`, and `test_sec_evidence_{storage,harvest,offline,expand,sweep}`.
- Pre-commit `make check`: **3,234 passed, 0 failures, 88.22% coverage**, exit 0;
  lint, formatting and strict mypy also passed. `make docs-build` passed.
- Runtime filesystem audit: **0 unjustified artifact leaks**, classified in
  [Storage layout](storage-layout.md#filesystem-search-audit).
- Read-only database SHA-256 checks matched the supplied baseline:
  - `compounder_radar.db`: `02a6660b81d96a948a1a5b8729290e301cbb9ab26155e5f07c3cf0b6beef7540`
  - `stage9k.db`: `20a77d24a4084caa49af723d488a1c9319562b32a34969b28780adfe898486e4`

### Phase 7F rollout — validated Stage C, against a copy

| | |
| --- | --- |
| Foreign cohort | 762 |
| Fundamentals recovered | 289 |
| Numerical scores | 206 |
| **Current-rank eligible** | **191** |
| Stale scored but excluded | 15 |
| Insufficient | 82 |
| Not eligible | 461 |
| Unsupported sector | 13 |
| Provider unavailable | 0 |

Cadence of the current-rank eligible: 191 `ANNUAL`, 0 `SEMIANNUAL`, 0
`QUARTERLY`. Taxonomy: 147 IFRS, 44 us-gaap.

Foreign entrants: **GFI (#11)** and **ERO (#19)** in the Top 20; DLO, ARIS, CYD,
KGC, IAG in the Top 50; TBBB, B, AU, MTA in the Top 100. Every Top-20 and Top-50
entrant was reconciled against its filed statements.

**CGAU keeps its 73.48 and leaves the ranking** — its newest statement is from
2023, against a 2026 market capitalisation.

### V1.1 → V1.2 parity

1,929 companies scored under both versions with **zero** numeric differences.
Every scoring file is byte-unchanged since the last V1.1 commit.

### Three defects the rollout exposed

Each was caught by running real companies, not by reasoning:

- The composite provider replaced the EDGAR profile wholesale, erasing the
  reporting currency only EDGAR knew. The profile is now merged field by field.
- The enrichment pass re-scored without an FX resolver, turning a complete
  `PRELIMINARY` score into a `FINAL` one missing every currency-sensitive metric
  — verification made the score *worse* for exactly the companies it verified.
- A deposit-funded bank (Kaspi.kz) scored 74.78 and ranked twenty-first because
  both available classifications called it a technology company.
  `domain.statements` now reads the statements instead.

### Liquidity pass economics

Running `update-eligibility-volume` before the metered pass took provider
requests for the 762-company cohort from **762 to 353**.

---

## Regression requirements

The B2 checkpoint adds `test_storage_contract.py`, `test_storage_local.py`,
`test_storage_equivalence.py` and `test_storage_neutral.py`: local/memory backend
equivalence, strict absence-versus-unavailability, unsafe-key refusal, parser
failure propagation, opaque ETags, reconciliation counters, and batch-local
sibling orphan detection. B2.5 adds `test_control_plane.py`: control-root
resolution without creation, unwritten-versus-gone for manifests and
checkpoints, the separated-root lifecycle, both failure-domain halves, the
legacy colocated layout, and backend-neutral preflight. Existing SEC evidence
regression suites remain gates. B2.6 adds `test_storage_configuration.py`:
explicit backend selection, no local fallback from `b2`, offline B2 validation,
and credential non-leakage across `repr`, errors, logs and status output. B2.7
adds `test_storage_b2.py` and the `b2` parameter of the contract and
reconciliation suites: the bucket backend must keep every promise the
directory backend keeps, offline.

These must keep passing. Each one encodes a decision that cost something to
learn.

| Test | Protects |
| --- | --- |
| `tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows` | the 1.6-million-row deletion never recurs |
| `tests/integration/test_migrations.py::test_the_migrated_schema_matches_the_models` | migrations and models cannot drift |
| `tests/integration/test_scoring_pipeline.py::test_a_current_company_scores_exactly_what_v1_1_scored` | V1.2 moved no number — the golden value was measured against the last V1.1 commit |
| `tests/integration/test_scoring_pipeline.py::test_every_current_ranking_view_excludes_a_stale_score` | the V1.2 policy applies to all four views |
| `tests/integration/test_scoring_pipeline.py::test_a_stale_company_keeps_its_score_and_leaves_the_ranking` | a stale score is withheld from rankings, not deleted |
| `tests/integration/test_scoring_pipeline.py::test_a_snapshot_predating_freshness_still_ranks` | pre-`0020` rows are not retroactively stale |
| `tests/integration/test_scoring_pipeline.py::test_a_bank_stays_in_the_database_and_out_of_the_ranking` | unsupported sectors are recorded, not dropped |
| `packages/domain/tests/test_audit_regressions.py` | ticker normalisation, bar validation, boundary inclusivity, consolidated-volume preference |
| `packages/domain/tests/test_scoring_v11_guards.py` | the three V1.1 guards, their stacking, and component maximums |
| `packages/domain/tests/test_period_cadence.py` | staleness scales with cadence; `UNKNOWN` is never reported stale |
| `packages/domain/tests/test_scoring_currency_safety.py` | an enterprise value without a conversion is rejected outright |
| `packages/domain/tests/test_statements.py` | a bank is recognised from its statements; a manufacturer with a finance arm is not |
| `packages/deep-research/tests/test_brief.py` | fingerprints are reproducible across reassembly and refetch |
| `tests/integration/test_pit_capture.py::test_a_completed_day_cannot_be_changed_by_a_later_score` | hindsight cannot rewrite the record — the property the whole phase rests on |
| `tests/integration/test_pit_capture.py::test_a_partial_run_leaves_no_day_behind` | a crashed capture leaves no half-day marked good |
| `tests/integration/test_migrations.py::test_the_identity_migration_preserves_a_populated_historical_security` | `0027` rebuilds a parent with three cascading children and deletes none of them |
| `packages/domain/tests/test_identity.py::TestATickerReuseIsADifferentSecurity` | two issuers under one symbol are never spliced into one history |
| `packages/domain/tests/test_identity.py::TestAReverseSplitPreservesTheSecurity` | a new CUSIP is a new interval, never a new security — and the retired one stops answering |
| `packages/domain/tests/test_identity.py::TestEvidenceAndItsAbsence` | `EvidenceVerdict` has no `ABSENT`: silence in the fails record can never become a claim |
| `tests/integration/test_security_identity.py::TestHistoricalSecuritiesStayOutOfTheLiveUniverse` | resolving a delisted symbol writes no `companies` row |
| `tests/integration/test_security_identity.py::TestForwardReturnsCarryExactlyOneIdentity` | a measurement names one identity, never both and never neither |
| `tests/integration/test_security_identity.py::TestDuplicateMeasurementsAreRefusedPerIdentityKind` | the partial unique indexes bite for each identity kind independently |
| `tests/integration/test_historical_outcomes.py::TestASecurityWithNoCompanyRow::test_a_historical_security_outlives_the_deletion_of_its_company_row` | the survivorship test — the live row is deleted and the outcome is unchanged |
| `tests/integration/test_historical_outcomes.py::TestTheMixedUniverseQuery` | survivors and delisted names come back from one query, no union and no flag |
| `tests/integration/test_historical_outcomes.py::TestIdentityIsNeverTheTicker` | two occupants of one symbol keep separate prices, outcomes and terminal events |
| `tests/integration/test_historical_outcomes.py::TestTerminalOutcomes::test_a_collapsing_price_never_becomes_a_terminal_value` | a falling tape is never read as a settlement |
| `tests/integration/test_historical_outcomes.py::TestTheScoreBoundary` | outcome availability and score availability stay separate concepts |
| `tests/integration/test_historical_outcomes.py::TestNoLookAhead` | a later interval, action or delisting cannot reach the decision-date state |
| `tests/integration/test_universe_membership.py::TestSurvivorsAndExits::test_membership_survives_the_deletion_of_every_company_row` | historical membership outlives the emptying of `companies` |
| `tests/integration/test_universe_membership.py::TestPricePresenceIsNotMembership` | Rule B — a priced security with no venue is `UNKNOWN`, never a member |
| `tests/integration/test_universe_membership.py::TestInferenceIsNeverDressedAsObservation` | no member verdict is ever written as observed, and each names what it carried |
| `tests/integration/test_universe_membership.py::TestFutureInformationCannotLeakBackward` | a 2024 listing is not a 2022 member; a future exit cannot rewrite the past |
| `tests/integration/test_pit_attribution.py::TestBedBathAndBeyondAreTwoOccupants` | the live holder of a reused symbol never owns its predecessor's months, and the predecessor never owns the successor's |
| `tests/integration/test_pit_attribution.py::TestALiveCompanyIsAClaimantNotADefault` | a stored exit date alone never moves a symbol off the live roster — the 127 same-business rows stay put |
| `tests/integration/test_pit_attribution.py::TestAPredecessorsFilingNeverClassifiesItsSuccessor` | the mirror defect: an old cover page cannot classify a later occupant's months |
| `tests/unit/test_sec_evidence_storage.py::TestTheRootIsNeverCreated` | an unmounted evidence root raises and leaves no directory on the boot disk |
| `tests/unit/test_sec_evidence_storage.py::TestAnArtifactIsWholeOrAbsent` | an interrupted write leaves nothing, and a destination outside the root is refused |
| `tests/integration/test_sec_evidence_harvest.py::TestAnArtifactIsNeverFetchedTwice` | a verified artifact is reused; a corrupt or unprovenanced one is re-fetched |
| `tests/integration/test_sec_evidence_harvest.py::TestBudgetsStopTheRunCleanly` | request and byte limits stop the run and the next run resumes |
| `tests/integration/test_universe_membership.py::TestIdentityIsNeverTheTicker` | ticker reuse gives two independent membership histories |
| `tests/integration/test_pit_capture.py::test_the_first_capture_records_a_baseline_not_a_dated_event` | no membership date is ever fabricated |
| `tests/integration/test_filing_observations.py::test_a_restatement_appends_and_the_original_survives` | a revision never destroys the figure it replaced |
| `tests/integration/test_filing_observations.py::test_period_end_is_never_used_as_availability` | the classic look-ahead trap stays shut |
| `packages/data-access/tests/test_pit_repositories.py` | immutability, append-only writes, and `filed_date` selection |
| `packages/domain/tests/test_features.py` | a named window used exactly its named length |
| `tests/integration/test_market_signals.py::test_a_stored_signal_cannot_change_when_operational_tables_move` | a derived signal cannot drift when the operational tables are rewritten |
| `tests/integration/test_market_signals.py::test_deriving_signals_cannot_alter_any_score` | the market layer is inert with respect to CompounderScore |
| `tests/integration/test_market_signals.py::test_a_new_version_appends_beside_the_old_one` | changing the rules never restates what was said at the time |
| `packages/domain/tests/test_signals.py` | percentile determinism, multi-label setups, and the tri-state verdict |
| `tests/integration/test_pit_metrics.py::test_a_restatement_is_invisible_until_it_is_filed` | the worked restatement: January sees nothing, March the original, May the revision |
| `packages/domain/tests/test_pricing.py::test_anchoring_on_the_valuation_date_would_undervalue_by_the_factor` | the wrong anchor is named, so reintroducing it fails rather than passes quietly |
| `packages/domain/tests/test_pricing.py::test_the_window_between_a_split_and_the_next_filing` | the two months where the newest filed count is on the old basis |
| `packages/domain/tests/test_pricing.py::test_the_valuation_is_continuous_across_the_split` | a company's size does not change because its shares were divided |
| `packages/domain/tests/test_outcomes.py::test_the_observation_session_is_never_the_entry` | a snapshot computable at a close is never filled at that close |
| `packages/domain/tests/test_outcomes.py::test_an_interrupted_outcome_has_no_return` | neither -100% nor 0% is assumed for a security that stopped trading |
| `packages/domain/tests/test_outcomes.py::test_a_one_day_hole_is_unpriced_not_interrupted` | our missing bars are never counted as delistings |
| `tests/integration/test_pit_outcomes.py::test_a_delisting_is_stored_without_a_company_row` | the delisting registry never pollutes the investable set |
| `tests/integration/test_pit_outcomes.py::test_without_a_benchmark_there_is_no_calendar_and_nothing_is_measured` | a security's own bars never stand in for the market's sessions |
| `tests/integration/test_pit_metrics.py::test_the_operational_table_cannot_influence_a_historical_metric` | a historical metric cannot drift when `financial_snapshots` is overwritten |
| `tests/integration/test_pit_metrics.py::test_a_metric_waits_for_its_slowest_input` | a multi-concept metric uses only facts available at the date |
| `tests/integration/test_pit_metrics.py::test_two_decision_dates_see_two_different_companies` | the end-to-end temporal scenario |
| `packages/api-clients/tests/test_edgar_observations.py::test_rebuilding_a_document_reproduces_the_same_periods` | the point-in-time path normalises identically to the live one |
| `packages/domain/tests/test_survivorship.py` | the delisting-reason taxonomy, the as-of membership rule, and the readiness gaps |
| `tests/integration/test_backtest_readiness.py::test_a_security_delisted_before_the_date_is_not_a_member` | a delisted name leaves the past universe — survivorship in its pure form |
| `tests/integration/test_backtest_readiness.py::test_a_filing_becomes_visible_only_after_it_was_filed` | the reconstructed score reads no fundamental before its filing date |
| `tests/integration/test_backtest_readiness.py::test_a_delisted_only_security_is_answerable_through_its_fate` | a security with no company row is answered through the registry, never dropped |
| `tests/integration/test_backtest_readiness.py::test_signals_backfill_for_a_past_date_from_stored_prices` | signal history is backfillable and point-in-time |
| `tests/integration/test_historical_securities.py::test_a_delisted_security_reconstructs_with_no_companies_row` | a delisted name is scoreable with no `companies` row |
| `tests/integration/test_historical_securities.py::test_a_namesake_company_cannot_change_the_historical_market_cap` | a reused ticker's live company cannot leak into a historical score |
| `tests/integration/test_historical_securities.py::test_a_future_split_does_not_leak_a_share_count_backward` | a split after the date corrects the price, never the count |
| `tests/integration/test_historical_securities.py::test_the_historical_path_scores_identically_to_the_company_path` | the historical path reuses the frozen scorer, not a second one |
| `tests/integration/test_historical_securities.py::test_a_missing_terminal_value_stays_none_never_zero` | an unpriced receivership stays UNKNOWN |

P2.2 adds `tests/integration/test_p2_2_external_paper.py` — 211 tests, all
against an injected fake transport, so the gate reaches no network. The rows
below are the ones whose absence would be expensive.

| Test | Protects |
| --- | --- |
| `test_p2_2_external_paper.py::test_no_environment_string_unlocks_live_trading` | no value of `environment` — including `production`, `prod`, `real`, `true` — reaches a live venue |
| `test_p2_2_external_paper.py::test_a_live_endpoint_is_refused` | a live host is refused even when the caller asks for paper |
| `test_p2_2_external_paper.py::test_an_unknown_host_is_refused_rather_than_assumed_paper` | the default for an unrecognised host is refusal, not permission |
| `test_p2_2_external_paper.py::test_a_live_key_prefix_is_refused_even_against_the_paper_host` | each of the three checks binds on its own |
| `test_p2_2_external_paper.py::test_exactly_one_mutating_request_on_every_path` | one POST per submission on all five outcome paths — the property that cannot double a position |
| `test_p2_2_external_paper.py::test_the_intent_is_durable_before_the_request_is_made` | a crash during the request leaves a record naming the order that may now exist |
| `test_p2_2_external_paper.py::test_a_lost_response_is_resolved_by_lookup_not_by_resending` | a timed-out POST whose order exists is found, not duplicated |
| `test_p2_2_external_paper.py::test_an_unresolvable_submission_stays_unknown` | `REMOTE_STATE_UNKNOWN` is never downgraded, and the intent survives |
| `test_p2_2_external_paper.py::test_a_locally_refused_order_leaves_no_intent_and_sends_nothing` | validation precedes durability, so a refusable order leaves nothing behind |
| `test_p2_2_external_paper.py::test_a_stale_display_ticker_is_never_used_as_a_fallback` | a reassigned ticker cannot route an order to another company |
| `test_p2_2_external_paper.py::test_two_identities_cannot_share_a_venue_symbol` | a remote fill is never attributed to whichever identity was found first |
| `test_p2_2_external_paper.py::test_an_unmapped_provider_status_is_refused` | the status map has no default branch to report a working order as dead |
| `test_p2_2_external_paper.py::test_a_pending_cancel_is_still_working` | a cancel asked for is not a cancel done |
| `test_p2_2_external_paper.py::test_the_same_execution_reported_ten_times_reads_as_one` | reading the venue repeatedly posts a fill once |
| `test_p2_2_external_paper.py::test_a_fee_the_venue_did_not_state_is_unknown_not_zero` | missing is not zero, at the one boundary where zero is usually right |
| `test_p2_2_external_paper.py::test_unreadable_positions_are_never_substituted_with_an_empty_list` | a failed read never reconciles as the book having vanished |
| `test_p2_2_external_paper.py::test_a_sweep_never_changes_the_book` | reconciliation reads three records and writes none |
| `test_p2_2_external_paper.py::test_the_reconciliation_module_exposes_no_repair_function` | no repair surface exists to be reached for later |
| `test_p2_2_external_paper.py::test_a_local_session_may_not_hold_a_broker_account` | a simulator session cannot hold a credential it would eventually use |
| `test_p2_2_external_paper.py::test_no_venue_is_live` | the venue enum has no live member and no third member to fall back to |
| `test_p2_2_external_paper.py::test_preflight_withholds_readiness_while_a_submission_is_unresolved` | an unknown order blocks the next plan rather than being planned around |
| `test_p2_2_external_paper.py::test_the_execution_core_imports_no_http_client` | network capability stays contained to `external/` |
| `test_p2_2_external_paper.py::test_a_report_is_credential_scanned_on_the_way_out` | no key, header, token or account number reaches a report |
| `test_p2_2_external_paper.py::test_the_v1_1_manifest_still_says_no_external_broker` | each manifest version keeps its own answers |
| `test_p2_2_external_paper.py::test_the_adapter_exposes_no_forbidden_convenience_api` | no liquidate, flatten, transfer, funding or live-enabling call exists |

P2.2C adds `tests/unit/test_alpaca_paper_transport.py` (80 tests, `httpx.MockTransport`,
no socket) and `tests/integration/test_p2_2c_paper_preflight.py` (27 tests). The
rows below are the ones whose absence would be expensive.

| Test | Protects |
| --- | --- |
| `test_alpaca_paper_transport.py::test_a_live_or_brokerage_host_is_refused_by_name` | the transport cannot be constructed against a live or brokerage host |
| `test_alpaca_paper_transport.py::test_anything_not_provably_the_paper_origin_is_refused` | a non-HTTPS scheme, userinfo, a non-443 port, an IP literal, `localhost` and an unknown host all fail closed |
| `test_alpaca_paper_transport.py::test_a_live_host_is_refused_before_any_request_is_made` | the refusal precedes any request, so no credential is ever sent to a live host |
| `test_alpaca_paper_transport.py::test_an_absolute_url_as_a_path_cannot_leave_the_locked_origin` | a client with a base_url still honours an absolute target; this is the check that stops it |
| `test_alpaca_paper_transport.py::test_the_production_client_is_locked_down` | redirects off and `trust_env` off — an environment variable cannot re-route a submission |
| `test_alpaca_paper_transport.py::test_a_redirect_is_refused_rather_than_followed` | a 3xx never moves an order request to another origin |
| `test_alpaca_paper_transport.py::test_a_timeout_leaves_a_mutation_unknown_not_failed` | the distinction the whole submission protocol rests on |
| `test_alpaca_paper_transport.py::test_a_failed_mutation_is_attempted_exactly_once` | no retry of a `POST`, on any failure class |
| `test_alpaca_paper_transport.py::test_the_transport_module_contains_no_retry_loop` | no retry surface exists in the module to be reached for later |
| `test_alpaca_paper_transport.py::test_a_decimal_in_the_payload_is_refused_before_anything_is_sent` | a local serialisation bug never becomes a network ambiguity that strands an intent |
| `test_alpaca_paper_transport.py::test_the_credentials_never_appear_in_a_classified_failure` | an exception's text is never rendered into a durable record |
| `test_alpaca_paper_transport.py::test_only_allow_listed_headers_are_recorded` | a `Set-Cookie` or `Authorization` response header is never retained |
| `test_alpaca_paper_transport.py::test_the_credentials_are_absent_from_the_evidence_fingerprint_body` | the evidence digest is over the response, never the credentialed request |
| `test_p2_2c_paper_preflight.py::test_the_preflight_makes_no_mutating_request` | the preflight is read-only, asserted by counting methods |
| `test_p2_2c_paper_preflight.py::test_another_account_withholds_readiness` | authenticating against some paper account is not proof it is this book's |
| `test_p2_2c_paper_preflight.py::test_an_account_mismatch_submits_nothing` | a mismatch stops before any mutation |
| `test_p2_2c_paper_preflight.py::test_the_artefact_carries_no_account_number` | neither the account id nor the account number reaches the artefact |
| `test_p2_2c_paper_preflight.py::test_the_artefact_states_what_it_does_not_establish` | the artefact discloses that no provider field attests paper-ness |

P2.2D adds `tests/integration/test_p2_2d_certification_repairs.py` (47 tests).
Each row below is a defect that passed the fake-transport suite.

| Test | Protects |
| --- | --- |
| `test_p2_2d_certification_repairs.py::test_the_fill_query_sends_the_provider_order_id` | the activities filter is the provider `order_id`, which is the only one the endpoint honours |
| `test_p2_2d_certification_repairs.py::test_the_fill_query_never_sends_a_client_order_id` | the parameter whose silent rejection caused the defect is never sent |
| `test_p2_2d_certification_repairs.py::test_the_fill_path_never_derives_a_client_order_id` | no fallback exists to be reached for when a filter appears not to work |
| `test_p2_2d_certification_repairs.py::test_an_unrelated_order_fill_on_the_same_page_is_refused` | an ignored filter cannot attribute another order's fill to this one |
| `test_p2_2d_certification_repairs.py::test_a_fill_in_an_unmapped_symbol_is_refused` | a fill that cannot be attributed to an identity is never posted |
| `test_p2_2d_certification_repairs.py::test_a_non_fill_activity_is_ignored_not_posted` | a dividend or fee on a mixed page is not an execution |
| `test_p2_2d_certification_repairs.py::test_polling_the_same_fill_repeatedly_yields_the_same_single_fill` | repeated retrieval stays exactly once |
| `test_p2_2d_certification_repairs.py::test_an_order_with_no_recorded_provider_id_cannot_have_fills_read` | fails closed rather than guessing an id |
| `test_p2_2d_certification_repairs.py::test_a_paper_proof_cannot_be_built_from_configuration` | the gate that could not fail cannot be satisfied by settings |
| `test_p2_2d_certification_repairs.py::test_a_paper_proof_from_a_live_origin_is_an_incident_not_a_proof` | a successful live-host reply is never paper evidence |
| `test_p2_2d_certification_repairs.py::test_a_paper_proof_binds_the_response_it_was_built_from` | the proof's digest is the account read's digest |
| `test_p2_2d_certification_repairs.py::test_the_gate_fails_without_a_proof` | an unapproved origin withholds the gate |
| `test_p2_2d_certification_repairs.py::test_the_preflight_reads_each_endpoint_exactly_once` | three requests, not six — the fingerprint describes the data reported |
| `test_p2_2d_certification_repairs.py::test_the_reported_values_and_the_fingerprints_come_from_one_response` | the audit binding the double-read destroyed |
| `test_p2_2d_certification_repairs.py::test_the_working_order_read_asks_for_open_orders` | `status=open`, so a count labelled "working" is working orders |
| `test_p2_2d_certification_repairs.py::test_the_preflight_never_fetches_order_history` | terminal history is not a reconciliation input |
| `test_p2_2d_certification_repairs.py::test_no_mutating_request_is_ever_repeated_by_the_adapter` | the cancel repair introduced no retry |
| `test_p2_2d_certification_repairs.py::test_the_adapter_sends_exactly_one_delete_per_cancellation_call` | one DELETE per call, structurally |
| `test_p2_2d_certification_repairs.py::test_cancellation_resolution_makes_no_mutating_request` | recovery reads; it never resends |
| `test_p2_2d_certification_repairs.py::test_the_frozen_v1_and_v1_1_code_sets_did_not_move` | V1 stays 40 and V1.1 stays 50, so the frozen manifests hold |
| `test_p2_2_external_paper.py::test_a_clean_delete_status_alone_does_not_prove_cancellation` | a 204 is not a terminal order |
| `test_p2_2_external_paper.py::test_an_order_that_filled_before_the_cancel_is_reported_filled` | a lost race is FILLED, never CANCELLED |
| `test_p2_2_external_paper.py::test_a_cancellation_intent_is_durable_before_the_delete` | a crash during the DELETE leaves a record naming the order |

P2.2E adds `tests/integration/test_p2_2e_paper_canary.py` (27 tests, all offline
against a fake venue that models the activities-feed lag the live run exposed).

| Test | Protects |
| --- | --- |
| `test_p2_2e_paper_canary.py::test_the_canary_survives_the_activities_feed_lagging` | an empty first activities read is not "no fill" |
| `test_p2_2e_paper_canary.py::test_the_canary_ingests_each_fill_exactly_once` | one accounting event per execution, duplicate poll admits nothing |
| `test_p2_2e_paper_canary.py::test_the_accounting_event_id_derives_from_the_provider_execution_id` | the derivation that makes re-reading safe |
| `test_p2_2e_paper_canary.py::test_a_pre_existing_position_blocks_the_canary` | an unaccounted holding stops the run before any order |
| `test_p2_2e_paper_canary.py::test_a_blocked_canary_submits_nothing` | a failed precondition means zero mutations |
| `test_p2_2e_paper_canary.py::test_the_canary_submits_exactly_two_orders` | entry and cleanup, and nothing else |
| `test_p2_2e_paper_canary.py::test_the_canary_never_cancels_anything` | cleanup is a trade, not a cancel |
| `test_p2_2e_paper_canary.py::test_the_canary_closes_what_it_opened` | no residual left behind |
| `test_p2_2e_paper_canary.py::test_two_runs_derive_different_client_order_ids` | the run tag, without which a second run adopts the first run's order |
| `test_p2_2e_paper_canary.py::test_the_run_tag_is_stable_within_a_run` | a retry after a crash stays idempotent |
| `test_p2_2e_paper_canary.py::test_an_unsuitable_instrument_is_refused` | halted, inactive and non-equity assets fail before an order exists |
| `test_p2_2e_paper_canary.py::test_asset_fields_default_to_not_permitted` | an omitted provider field reads as "not permitted" |
| `test_p2_2e_paper_canary.py::test_fractional_is_declared_from_the_asset_read` | the capability follows the instrument, not a venue default |
| `test_p2_2e_paper_canary.py::test_the_artefact_states_it_is_not_live_trading` | every artefact carries the canary classification |
| `test_p2_2e_paper_canary.py::test_the_artefact_carries_no_credential_or_account_number` | nothing identifying reaches the certification record |
| `test_p2_2e_paper_canary.py::test_the_artefact_does_not_claim_reproducibility` | the honest reproducibility language survives |
| `test_p2_2e_paper_canary.py::test_a_residual_is_closed_by_a_compensating_order` | a stranded position is traded out, not liquidated |

P2.3 adds `tests/integration/test_p2_3_paper_operations.py` (120 tests, all
offline against a stub environment and an injected clock).

| Test | Protects |
| --- | --- |
| `test_p2_3_paper_operations.py::test_a_fresh_install_is_killed` | absent control state is never permission |
| `test_p2_3_paper_operations.py::test_corrupt_control_state_fails_closed` | an unparseable kill switch resolves to killed |
| `test_p2_3_paper_operations.py::test_a_tampered_kill_state_fails_closed` | a forged ACTIVE state is refused |
| `test_p2_3_paper_operations.py::test_a_kill_survives_a_restart` | a restart is not a recovery |
| `test_p2_3_paper_operations.py::test_resuming_after_a_kill_needs_recovery_not_a_restart` | leaving a kill needs reconciliation and no open blocking incident |
| `test_p2_3_paper_operations.py::test_a_kill_blocks_submission_and_liquidates_nothing` | a kill is not a liquidation |
| `test_p2_3_paper_operations.py::test_an_unresolved_owned_order_is_worse_than_a_plain_kill` | the two states are never collapsed |
| `test_p2_3_paper_operations.py::test_an_authorization_cannot_name_a_live_environment` | no live operations mode exists |
| `test_p2_3_paper_operations.py::test_an_authorization_cannot_cover_any_account` | a wildcard grant would survive a repointed credential |
| `test_p2_3_paper_operations.py::test_arming_states_it_authorises_no_order` | arming permits a coordinator, not a trade |
| `test_p2_3_paper_operations.py::test_an_observe_only_grant_does_not_permit_paper_execution` | a narrow grant is not widened at runtime |
| `test_p2_3_paper_operations.py::test_a_second_invocation_for_the_same_slot_is_a_no_op` | one scheduled decision never becomes two orders |
| `test_p2_3_paper_operations.py::test_a_stale_lease_is_not_taken_on_age` | a crashed run may already have submitted |
| `test_p2_3_paper_operations.py::test_a_killed_run_has_no_legal_exit` | the transition table forbids resuming after a kill |
| `test_p2_3_paper_operations.py::test_an_executing_run_cannot_go_back_to_planning` | no edge re-submits orders already at the venue |
| `test_p2_3_paper_operations.py::test_there_is_no_degraded_path_to_a_mutation` | only HEALTHY permits a submission |
| `test_p2_3_paper_operations.py::test_health_fails_closed_when_a_check_did_not_run` | silence is not a pass |
| `test_p2_3_paper_operations.py::test_a_provider_outage_blocks_and_never_falls_back` | the local simulator is never a fallback |
| `test_p2_3_paper_operations.py::test_a_stale_input_blocks_the_run` | an input existing is not evidence it describes the market |
| `test_p2_3_paper_operations.py::test_an_input_exactly_at_the_limit_is_fresh` | the freshness boundary is explicit and consistent |
| `test_p2_3_paper_operations.py::test_a_future_dated_input_is_not_fresh` | a future as_of is reported, not clamped |
| `test_p2_3_paper_operations.py::test_a_missed_slot_is_recorded_and_never_run` | no backfill trading |
| `test_p2_3_paper_operations.py::test_a_kill_during_the_run_stops_before_the_first_order` | permission is re-read, not cached |
| `test_p2_3_paper_operations.py::test_a_kill_between_two_orders_keeps_the_first_and_stops_the_second` | the critical mid-run case |
| `test_p2_3_paper_operations.py::test_the_submission_gate_runs_once_per_order` | the gate is per submission, not per run |
| `test_p2_3_paper_operations.py::test_remote_state_unknown_blocks_the_whole_account` | ambiguity is not isolated so other names keep trading |
| `test_p2_3_paper_operations.py::test_a_reconciliation_break_blocks_and_is_never_repaired` | no operational auto-repair |
| `test_p2_3_paper_operations.py::test_a_blocking_incident_cannot_resolve_itself` | a system that clears its own alarms has none |
| `test_p2_3_paper_operations.py::test_re_detecting_a_condition_touches_one_incident` | a hundred copies of one problem do not read as a hundred problems |
| `test_p2_3_paper_operations.py::test_the_run_journal_detects_truncation` | the anchor catches what the chain cannot |
| `test_p2_3_paper_operations.py::test_an_illegal_transition_is_refused_before_it_is_written` | the P1.4 pre-write lesson |
| `test_p2_3_paper_operations.py::test_operational_state_cannot_be_written_into_another_domain` | control state never shares a root with evidence or ledgers |
| `test_p2_3_paper_operations.py::test_a_blocked_run_is_not_reported_as_a_success` | a correct refusal is not an achievement |
| `test_p2_3_paper_operations.py::test_the_execution_and_accounting_versions_are_not_bumped` | operations did not move execution or accounting semantics |

P2.4 adds `tests/integration/test_p2_4_paper_shadow_soak.py` (58 tests) and
`tests/integration/test_p2_4_required_drills.py` (26 tests), all offline.

| Test | Protects |
| --- | --- |
| `test_p2_4_paper_shadow_soak.py::test_a_session_before_activation_cannot_count` | the prospective boundary, which is the whole design |
| `test_p2_4_paper_shadow_soak.py::test_a_frozen_protocol_cannot_be_replaced` | the goalposts cannot move after the first observation |
| `test_p2_4_paper_shadow_soak.py::test_a_soak_cannot_be_activated_twice` | one armed soak, not a series of attempts |
| `test_p2_4_paper_shadow_soak.py::test_the_gates_live_inside_the_protocol_fingerprint` | a changed threshold changes the fingerprint |
| `test_p2_4_paper_shadow_soak.py::test_the_report_has_no_pass_rate_field` | 19 of 20 clean is a failed soak, not 95% |
| `test_p2_4_paper_shadow_soak.py::test_nineteen_clean_sessions_and_one_duplicate_order_fails` | one zero-tolerance breach is not averaged away |
| `test_p2_4_paper_shadow_soak.py::test_an_observation_is_not_overwritten` | evidence appends; corrections supersede |
| `test_p2_4_paper_shadow_soak.py::test_recording_the_identical_observation_twice_is_a_no_op` | a retry is safe because the id is content-addressed |
| `test_p2_4_paper_shadow_soak.py::test_a_correction_preserves_the_original` | a correction cannot silently replace what it corrects |
| `test_p2_4_paper_shadow_soak.py::test_an_eligible_session_with_no_record_blocks_a_pass` | missing evidence blocks an A without being a breach |
| `test_p2_4_paper_shadow_soak.py::test_a_missed_session_without_a_trade_is_not_a_failure` | a correct refusal to trade is the system working |
| `test_p2_4_paper_shadow_soak.py::test_eligible_sessions_are_computed_from_the_calendar` | the denominator is derived, never counted by hand |
| `test_p2_4_paper_shadow_soak.py::test_the_protocol_states_what_it_does_not_gate_on` | performance is not a gate, so results cannot tune the protocol |
| `test_p2_4_paper_shadow_soak.py::test_a_hand_edited_observation_is_refused` | a session cannot be made clean by editing JSON |
| `test_p2_4_paper_shadow_soak.py::test_the_report_is_rebuilt_from_observations_not_a_summary_file` | the verdict is recomputed from evidence, never cached |
| `test_p2_4_paper_shadow_soak.py::test_a_corrupt_soak_state_never_reads_as_completed` | corrupt state fails closed, like every other control state |
| `test_p2_4_paper_shadow_soak.py::test_no_soak_artefact_carries_a_credential` | nothing identifying reaches a soak record |
| `test_p2_4_required_drills.py::test_every_required_drill_passes` | the six failure paths actually behave as the soak assumes |
| `test_p2_4_required_drills.py::test_a_drill_that_does_not_behave_as_expected_raises` | a failing drill stops arming instead of being filed |
| `test_p2_4_required_drills.py::test_a_failing_drill_is_still_recorded` | refusing is not the same as hiding |
| `test_p2_4_required_drills.py::test_the_kill_survives_a_new_store_over_the_same_root` | a restart is not a recovery |
| `test_p2_4_required_drills.py::test_the_provider_unavailable_drill_blocks_rather_than_substituting` | an outage blocks; the simulator is never substituted |
| `test_p2_4_required_drills.py::test_the_remote_state_unknown_drill_blocks_the_whole_account` | ambiguity is not isolated |
| `test_p2_4_required_drills.py::test_a_drill_never_writes_outside_its_own_root` | a drill cannot alter the state the soak is judged on |
| `test_p2_4_required_drills.py::test_no_soak_command_offers_a_live_flag` | no runtime flag unlocks live trading |
| `test_p2_4_required_drills.py::test_the_declared_soak_calendar_lists_no_declared_closure` | a closure listed as a session would strand the soak forever |
| `test_p2_4_required_drills.py::test_no_declared_session_closes_before_the_soak_would_run` | no session is scheduled into a market that already shut |
| `test_p2_4_required_drills.py::test_the_soak_run_time_is_inside_every_declared_session` | the run time holds on all 76 declared sessions |
| `test_p2_4_required_drills.py::test_the_first_eligible_session_is_the_next_declared_one` | the day somebody arms on is usually not itself a session |
| `test_p2_4_required_drills.py::test_the_operational_root_is_not_committable` | `git add -A` must never stage kill state or an account fingerprint |

### The gate

`make check` — lint, format check, mypy strict, pytest with coverage failing
under **80%**. It is what CI runs; run it rather than the individual tools.
`make check-web` is separate and needs Node. Touching `frontend/` means both.

---

## Next phase

Two tracks are pending, on different clocks.

**On the execution track, the transport now exists and three adapter defects
block certification.** P2.2C delivered `AlpacaPaperHttpTransport` and the
read-only preflight command; what remains is a defect-fix phase, then
certification.

**P2.2 is complete and certified.** The transport exists, the four certification
defects are closed, the read-only preflight passes, and one authorised order has
been through the full chain to a real paper venue and back.

**P2.3 is complete** at tag `p2-paper-operations-complete` (`68709d22`). The
control plane exists and is tested offline.

**P2.4's first soak is halted at 0 of 20, permanently.** The protocol froze and
armed correctly and the drills passed; the first eligible session exposed that
no concrete `OperationsEnvironment` exists to run it. The V1 protocol
`4b50f8b4…` is superseded, its evidence preserved unmodified, and it may never
be resumed or its fingerprint reused.

**The blocking phase is strategy definition, not integration.** A concrete
operations environment can orchestrate the chain from portfolio construction
onward, because every one of those authorities exists. It cannot supply the
first link. Until something in this repository is authorised to turn the
universe, the scores, the signals or the research record into a `StrategyIntent`
— with its own point-in-time rules, its own version and its own approval — there
is nothing for an operational run to gate. That phase must name the strategy,
not have one appear as a side effect of wiring.

**What finishing P2.4 requires.** Each eligible session produces one observation
recorded through `SoakStore.record_observation` — run status, orders, fills,
reconciliation verdict, health state and the incidents raised. Nothing is
backfilled and nothing is hand-edited; an eligible session with no record stays
visible as a gap until a person explains it. `paper-ops soak status` scores what
exists against the frozen protocol and will keep returning
`B_SOAK_IN_PROGRESS` until the evidence is complete.

**What would end the soak early, and what would not.** A zero-tolerance breach —
a duplicate remote order, a duplicate accounting event, a trade on stale data, a
backfilled run, an ignored reconciliation break — ends it as
`FAIL_SAFETY_VIOLATION` and requires a fix and a *new* soak, because a protocol
cannot be re-armed. A blocked session, a missed session, or a session where the
control plane refused to trade does not end it and is not a defect; it is the
system working, and it blocks only the `A`.

**What a clean soak does not authorise.** `A_PAPER_SHADOW_SOAK_PASSED` means the
control plane operated as specified over the preregistered window. It is not a
live trading authorisation and creates no path to one. Any widening past shadow
paper operation is a separate decision with its own evidence, and the thresholds
for it are not in this protocol.

Nothing in P2.4 relaxes what P2.2 and P2.3 established, and nothing in finishing
it may: no live host, no live key prefix, no third venue or mode member, no
retryable mutation, no auto-repair, no venue fallback, no backfill, an
operational default of `NOT_ARMED`, and `make check` continuing to make no
external request. The soak protocol itself adds two more: thresholds may not be
tuned from observed outcomes, and no observation may be recorded for a session
that closed before activation.

Neither phase may relax anything P2.2 or P2.2C established: no live host in the
allow-list, no live key prefix accepted, no third `ExecutionVenue` member, no
mutating request made retryable, no `trust_env=True`, and `make check`
continuing to pass no live flag and make no external request. A credential
belongs in `config.py` and `.env.example` by name with a placeholder value,
never in code, a fixture or a test.

**R1.8 — capture the first eligible prospective H2 decision when due** follows
on the research track.
The runner is armed and waiting; R1.8's job begins at the derived decision
instant, 16:00 America/New_York on the first eligible quarter-end session, and
not before.

**What R1.8 must do.** Build the decision-time source snapshot — universe,
fundamentals with acceptance timestamps, prices through the decision session,
score inputs, tradability — and prove every input was available at or before the
instant. Produce one ranked cross-section, feed it to both strategies, check the
initial-formation invariant that H2's target set equals the comparator's when
there are no incumbents, and finalise one immutable decision packet. Then, on
the next session, capture the reference open as a separate execution packet that
links to the decision by digest and modifies nothing.

**What R1.8 must not do.** It must not capture early: a packet may only be
finalised at or after the decision instant. It must not convert later-current
data into an as-of snapshot — if availability cannot be proven under the
prospective contract, the capture fails closed and the observation does not
count. It must not write an execution price into a decision packet, rewrite a
finalised packet, or reconstruct a missed quarter. It must not connect a broker
or place an order of any kind. And it must not report a mechanism reading: the
criteria stay sealed until four transitions are in.

**What still cannot be claimed, whatever happens.** One decision is not a
window, four transitions cannot establish a return effect, and the historical
support remains exploratory and unupgradable on the frozen dataset. The
candidate stays `PROSPECTIVE_CONFIRMATION_PENDING` until the window closes.

Nothing here is ready to trade. The current-classification limitation remains
material and unresolved, no prospective observation exists, paper and live
trading are unauthorised at every level of the ladder, and the legacy validation
block still trails its benchmark by 7.4 percentage points under both strategies.

No evidence acquisition, no A4, no change to the PIT, identity or scoring
semantics, and no promotion of `MARKET_SIGNALS_V2` without its own decision.
FTD archives are redownloaded separately. The one-month boundary between the
classification and planning rules remains documented in
[A3 planning inputs](a3-planning-inputs.md) and is a decision for a later
phase; widening the acquisition window is likewise a separate, versioned
decision.

Two pieces of work are *identified* but not authorised, and neither is to be
started without being asked:

1. **Promoting the Phase 7F cohort to the primary database.** The code is done;
   the data operation is not. It means applying the migrations and running the
   ordinary passes against `compounder_radar.db`.
2. **Phase 7D — period semantics.** Annual and semiannual reporting periods as
   first-class citizens. This is what still keeps TSM, ASML, SAP and NVO
   unrankable.
3. **Point-in-time market capitalisation.** Nothing records what it was on a past
   date, so any ratio against it is point-in-time on its fundamental side only.
   Share counts are now in the observation record, so it is derivable — but only
   once corporate actions exist, because stored closes are split-adjusted.
4. **Filing acceptance time.** `filed_date` is a date; 60.3% of same-day filings
   are accepted after the close. See the Phase 8E audit.

A longer-term direction document exists at `docs/reference/roadmap.md`. It is
aspiration, not commitment: nothing in it is authorised, scoped or scheduled, and
nothing in it overrides *Things explicitly not to change*.

---

## Things explicitly not to change

Not a backlog. These exclusions are deliberate.

**Do not build without being asked:**

- a live broker adapter, or an external paper brokerage of any kind — not even
  read-only. The broker is deterministic and local, and external brokerage comes
  only after the internal semantics are frozen
- randomness in a paper fill, under any name
- a market-impact model, latency simulation, queue position, hidden liquidity,
  venue routing or auction behaviour — each is a new engine version, never an
  edit to V1.1
- any execution-parameter optimisation, or a friction value calibrated from a
  strategy's returns
- a negative slippage, a price-improvement branch, or a participation ceiling
  above 1
- reviving an expired DAY order, or any order that spans two sessions
- a next-session rule other than the explicit calendar — never `date + 1`
- posting spread, slippage or shortfall into the accounting book as an expense:
  the price the book paid already contains it
- TWAP, VWAP scheduling, participation algorithms, smart routing or icebergs
- a broker that maintains its own cash, positions, P&L or buying power
- an unauthorised-order path: no `force`, no `unsafe_submit`, no arbitrary submit
- an accounting path that writes a risk target into a holding, or any `apply` /
  `execute` seam between `portfolio_risk` and `portfolio_accounting`
- a reconciliation auto-repair, an "adopt external" flag, or a cash tolerance
  defaulted to anything but zero
- **live trading, in any form**: no `USE_LIVE`, no `--live`, no `mode=live`, no
  `fallback_to_live`, no live host in the allow-list, no live key prefix accepted,
  and no third `ExecutionVenue` member
- treating an unrecognised host, key prefix or environment name as paper: the
  default is refusal, because the cost of guessing wrong is a real order
- a retry of a mutating broker request, or any generic retry helper that could be
  pointed at one
- a generated client order id — a UUID, a counter, or anything carrying a clock:
  it must be derived from the local order id so a crash cannot orphan an order
- downgrading `REMOTE_STATE_UNKNOWN` to `NOT_SUBMITTED`, or submitting an order
  while its earlier intent is unresolved
- a fallback from an identity's declared venue symbol to its display ticker, or a
  default branch in the provider status map
- substituting zero for a fee the venue did not state, or an empty list for a
  position read that failed
- an account funding, transfer, close-all, liquidate, flatten, replace or amend
  call, or a streaming subscription
- persisting or logging a credential, authorization header, token or cookie, or
  writing an account number into a report
- an HTTP client imported anywhere in `execution/` outside `execution/external/`
- `trust_env=True` on the paper transport's client, or any client construction
  that lets `HTTPS_PROXY` / `ALL_PROXY` re-route a submission
- `follow_redirects=True` on the paper transport, or following a 3xx on any
  mutation
- a second `BrokerTransport` implementation that is not host-locked in its
  constructor, or any way to re-point an existing transport after construction
- a retry loop inside the transport module, for any HTTP method
- classifying a local failure — a serialisation error, a host-lock violation —
  as a transport outcome: it would be marked ambiguous and strand an intent
- rendering an exception's message, a request URL, or any request header into a
  detail string, an evidence body or an artefact
- fingerprinting a request: the evidence digest is over the response only,
  because the request carried the credential
- a response-header deny-list in place of the allow-list
- reusing `alpaca_api_key` / `alpaca_secret_key` as paper trading credentials:
  they are a market-data credential and `alpaca_trading_base_url` defaults to the
  live host
- making `ALPACA_PAPER_ACCOUNT_ID` optional: authenticating against some paper
  account proves nothing about whether it is this book's
- filtering provider fill activities by anything but the provider `order_id`, or
  adding a client-order-id fallback to that path
- admitting a returned activity without checking it names the order it was
  requested for: an ignored query filter is invisible in the response
- adopting a cancellation from a mutation's status code instead of reading the
  order back, or cancelling by any identifier but the provider order id
- a paper-environment gate satisfiable from configuration, or any constructor on
  `ProviderPaperEnvironmentProof` that takes `Settings`
- reading an endpoint a second time to fingerprint it: the digest must come from
  the response whose values are reported
- labelling a `status=all` order listing as open or working orders
- clearing, cancelling or liquidating pre-existing paper positions the user owns
- a third `OperationsMode` member, or any live operations mode
- an operational default other than `NOT_ARMED`, or a missing/corrupt control
  state that resolves to anything but `KILLED`
- an arming grant that authorises a security, a quantity or an order, or one
  covering a wildcard account
- a kill that liquidates a position, cancels an order it cannot prove it owns,
  or requires the provider to be reachable before becoming durable
- resuming from `KILLED` by restarting a process, or any outgoing edge from
  `RunState.KILLED` in the transition table
- caching kill-switch permission for the duration of a run instead of re-reading
  it before every order
- a generated (non-derived) operations run id, or a run id that includes a clock
- taking a stale run lease on age rather than recovering the recorded run
- a `DEGRADED` health state that permits a mutation, or treating a required
  check that did not run as passed
- falling back to `LOCAL_DETERMINISTIC_PAPER` when the external venue is
  unreachable
- backfilling a missed scheduled slot, or submitting a plan built for an earlier
  session
- isolating a `REMOTE_STATE_UNKNOWN` so other names keep trading
- an incident that resolves itself automatically, or any operational auto-repair
- widening `CRITICAL_AUTO_KILL_REASONS` to ordinary trouble such as a stale
  input or an unreachable provider
- writing operational control state into the evidence, research, accounting or
  execution-journal namespaces
- a control-state write that is not read back and verified
- closing a canary position with a cancel, a close-all or a liquidation instead
  of a compensating authorised order
- a canary that calls the transport directly to trade rather than originating
  from an accounting-V2 book and an authorised plan
- concluding "no fill" from one empty activities read: the venue reports an order
  filled before its FILL activity is queryable
- a randomly generated canary plan id: it must be run-scoped and derived, so a
  retry within a run stays idempotent
- a venue-level fractional default standing in for the instrument's own
  `fractionable` flag
- substituting an observed fee for a declared one, or letting a declared cash
  tolerance change the global default away from zero
- a session that mixes `LOCAL_DETERMINISTIC_PAPER` and `EXTERNAL_PAPER` fills, or
  any fallback from one venue to the other
- widening `EXECUTION_MANIFEST_V1_1` to describe the external adapter: a V1.1
  engine has none, and each manifest version keeps its own answers
- any claim that a paper fill demonstrates a trade was achievable
- a second supported corporate action, tax lots, FIFO or specific-lot
  identification — each is a new cost-basis version, never an edit
- a SQLite or SQLAlchemy ledger backend; the file format is what makes the
  append-only guarantee structural rather than a matter of discipline
- an automatic V1-to-V2 accounting migration, or any migration from a V1
  snapshot: the per-owner bases are not in it and must not be fabricated
- a head anchor that can move backward, or an optional-anchor commit path
- a network client inside `portfolio_accounting`; a remote external anchor
  implements `ExternalAnchorSink` outside the package and is injected
- any claim that local files alone make the ledger rollback-proof
- a second LLM provider, or a second execution path for a job
- scheduled or automatic deep research, report diffing, alerts, notifications
- charts, portfolios, positions, price targets
- authentication or deployment config
- filing exhibits, 6-K exhibit discovery
- converting *statements* into another currency
- annual or semiannual period semantics, ADR ratio lookup
- reading filing documents to recover figures the companyfacts API omits

**Do not touch:**

- B2.4 through B2.7 leave `COMPOUNDER_V1_2`, `MARKET_SIGNALS_V1`,
  `ResearchUniverseMonth`, PIT identity semantics, Plan A3, forward returns,
  price history and database schema frozen. No BEK access, no credentials
  created, no corpus migration. B2.7 adds `boto3` as the one cloud SDK.
- **The SEC/PIT evidence program is closed (B2.18).** No SEC evidence
  acquisition — no baseline expansion, A3 continuation or A4, no ticker-map
  or submissions refresh for the frozen window — without an independently
  reviewed correctness defect. `residual_sec_forensic_plan.jsonl` is never
  executed. `PIT_RESEARCH_DATASET_V2` and `BACKTEST_DATA_CONTRACT_V1` are
  read through `load_contract`; research code that bypasses the contract's
  bindings is a defect.
- **The R1.2 result and its criteria (R1.2).** The five falsification criteria
  and their thresholds are frozen and were not revised after the measurements.
  `COMPOUNDER_QUARTERLY_TOP_DECILE_V1` is not re-specified, and no second
  rebalance frequency, selection fraction, weighting, cost level, buffer or
  minimum-holding rule is tried on the strength of R1.3's findings.
  2025-01…2026-03 is never described as confirmation of H1.
- **The prospective runner and its activation (R1.7).** The activation record
  and the arm record are written once and never rewritten; the activation instant
  comes from the runtime clock and cannot be supplied; the first eligible
  decision is derived by the protocol and never manually assigned. The runner
  must never gain an order-submission capability, a `force` parameter, or a
  broker credential, and `NYSE_SESSION_CALENDAR_V1` must keep reproducing every
  frozen benchmark session. A missed arm cutoff skips its quarter permanently —
  a reconstruction is a backtest and may never enter the confirmatory sample.
  Prospective state stays out of `research/r1_*`.
- **The H2 candidate and its prospective protocol (R1.6).**
  `H2_MEMBERSHIP_STABILITY_CANDIDATE_V1` is frozen with `ONE_DECILE_HYSTERESIS_V1`
  unchanged, and historical development on it is closed: no further retention
  width, frequency, weighting or holding rule under this candidate id.
  `H2_PROSPECTIVE_SHADOW_PROTOCOL_V1` fixes four transitions, four paired
  criteria and the rule `P1 AND P2 AND P3 AND P4` before any observation; none
  of these may be revised once the window opens, the historical 28.23% and the
  legacy 1.95× are never prospective thresholds, and performance never enters
  the verdict. No historical subperiod is relabelled, no reconstruction counts as
  prospective, and no frozen decision packet is ever rewritten. Broker paper and
  live capital are unauthorised, and `prospective_protocol` must never gain an
  import that lets it simulate, fetch or trade.
- **The R1.5 result and its criteria (R1.5).** The four criteria, their
  thresholds and the aggregate rule were fixed before execution and are not
  revised after it. `ONE_DECILE_HYSTERESIS_V1` is not re-specified, no second
  band width, cadence, weighting, holding period, fixed position count, turnover
  penalty or score smoothing is tried under the H2 name, and the specification
  is not run again — `r1_5_execution_state.json` refuses it. The result is never
  described as confirmation, out-of-sample or a clean holdout, and 1.95× stays
  out of the verdict. Any new rule needs a new hypothesis id and its own
  preregistration.
- **The H2 preregistration (R1.4).** `H2_MEMBERSHIP_STABILITY` and
  `ONE_DECILE_HYSTERESIS_V1` are frozen: one entry threshold, one retention band
  of two selection widths, one frequency, one weighting, one execution rule, one
  cost level. The four criteria, their thresholds and the aggregate rule
  `C1 AND C2 AND C3 AND C4` are fixed and are not revised after the result.
  1.95× is a diagnostic and never an H2 criterion. No historical subperiod is
  relabelled as an H2 holdout, and a historical H2 run is never called
  out-of-sample, clean-holdout or prospective confirmation. `verify_h2` enforces
  all of it and has no `--force`; `membership_hypothesis` must never gain an
  import that lets it price or simulate.
- **The R1.3 forensic record and what it withheld (R1.3).** The turnover
  definition, the membership-change floor and the cause attribution stand as
  measured; the C3 threshold is not altered retrospectively; R1.2's
  four-of-five is not re-read as H1 passing, because the specification states no
  aggregate rule and none has been invented. Above all, **no numeric rule may be
  taken from R1.3's boundary or rank-move distributions.** R1.3 chose none on
  purpose — they are the outcome a stability rule would be judged against — and
  R1.4 must state its rule before evaluating it. `turnover_forensics` must never
  gain an import that lets it simulate.
- **The R1.1 hypothesis and the R1.2 specification (R1.1).**
  `r1_1_hypothesis.json` and `r1_2_experiment_spec.json` are frozen. Their
  rules, thresholds and falsification criteria are not edited after the fact,
  and R1.2 does not try a second frequency, fraction, weighting or cost level
  alongside the one they name. The 2025-01…2026-03 interval is never described
  as untouched confirmation of anything derived after R1.0 read it.
- **The R1.0 research protocol (R1.0).** `research_split_v1.json` and
  `r1_0_preregistration.json` are frozen: their boundaries, strategy
  definitions, execution rule, cost assumption, terminal-leg policy, metric
  set and benchmark are not edited after the fact, and a changed definition is
  a new strategy version with its own preregistration. The holdout is not
  re-read under a definition altered after seeing it. No parameter, threshold,
  top-N, horizon or cost-level search; no signal combination; no sector logic;
  no second cost level reported in place of the preregistered one. Negative
  results stay in `research_ledger.jsonl`.
- any scoring file, without a new `score_version` and a doc change in the same
  commit
- a Phase 3 enum or class — `deep-research` imports them, never extends them
- `uv.lock` by hand — use `uv add` / `uv remove`
- `.github/workflows/` — ask first
- the frontend's location — it stays in this repository
- `compounder_radar.db`, for any migration verification

**Never:**

- read `os.environ` directly — add a typed field to `config.py` and a line in
  `.env.example`
- commit a secret, in code, tests, fixtures or docs
- leave a failing or skipped test without saying so explicitly
- let a calculated figure become indistinguishable from a supplied one

---

## Latest checkpoint

| | |
| --- | --- |
| Commit | The R1.10 commit containing this checkpoint, `research: certify and arm H2 prospective V2 window`, which the V2 activation and arm bind to; parent `92cc56f` (R1.9, `feat: build prospective shadow data plane`), then `85a63c6` (R1.7, `research: activate H2 prospective shadow runner`, which the prospective activation record binds to), then `e730e93` (R1.6), `d825a3b` (R1.5), `f7332d0` (R1.4), `1c4fa88` (R1.3), `db96f18` (R1.2), `27a5cb7` (R1.1), `196690d` (R1.0) and `06357c5` (B2.18, tagged `pit-research-dataset-v2`). The authoritative plan's meta and authorization record name `repo_commit` `daa1d36`; the corrected package records `2a7d3b8`, the superseded package `ebfbc1c`, the superseded plan `afe6a10`, the index `1b407b6`. |
| Branch | `feat/r1-backtest-harness`, branched from `feat/phase-b2-storage-abstraction` at `06357c5` |
| Migration head | `0031` — the research-universe layer (9K.7). Nothing since 9K.11 added one. |
| Score version | `COMPOUNDER_V1_2` (unchanged by anything in Phase 8, 9A, 9B or 9C) |
| Feature version | `MARKET_FEATURES_V1` |
| Signal version | `MARKET_SIGNALS_V1` |
| Primary database | untouched by 8A–8J, 9A–9L.5, B2 and R1.0–R1.10; all work ran against staging copies |
| Checkpoint date | 2026-09-25 |
