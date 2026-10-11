"""The twenty-four derivations themselves.

Separated from `derivations.py` so the framework and the implementations can be
read independently: the framework decides what a derivation *is* and how it
fails closed, this decides what each gate actually means in terms of records.

Two patterns recur and are worth naming once.

**Pairing, not counting.** Most safety invariants are not "how many of X" but
"did X happen when Y forbade it". A stale input is not a breach; a stale input
*and* an order is. A reconciliation break is not a breach; a break followed by a
mutation is. So these derivations read two kinds of record from one run and
compare them, which is why the operations journal — sequence-ordered, hash
chained, one run id per run — is the source almost all of them need.

**Conditional sources, cross-checked.** Some sources do not exist until
something happens: no incident directory until an incident, no request audit
until an audit store is supplied. An absent source is only an observed zero if
something *else* proves nothing happened. Where it does not, the derivation
returns `EVIDENCE_MISSING`, because "the file isn't there" and "it didn't
happen" are different facts and only one of them is safe to promote on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .derivations import (
    Derivation,
    DerivationContext,
    DerivedMetric,
    GateScope,
    MetricStatus,
    _evidence,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["build"]

#: Tolerance for a float weight comparison. Risk returning the requested weight
#: exactly must not read as an increase; see P2.3.1C.
_WEIGHT_TOLERANCE = 1e-9


def _missing(
    derivation: Derivation,
    context: DerivationContext,
    gate_id: str,
    *why: str,
) -> DerivedMetric:
    """A metric whose sources could not answer. Never a zero."""
    return DerivedMetric(
        gate_id=gate_id,
        status=MetricStatus.EVIDENCE_MISSING,
        value=None,
        derivation_id=derivation.derivation_id,
        derivation_version=derivation.version,
        scope=derivation.scope,
        source_schemas=derivation.required_schemas,
        session=context.session,
        protocol_fingerprint=context.protocol_fingerprint,
        qualifying_commit=context.qualifying_commit,
        detail=tuple(why),
    )


def _not_started(context: DerivationContext) -> bool:
    """Whether this soak has genuinely not begun.

    An absent operations journal means one of two very different things. If the
    soak also has no observations, nothing has run and there is nothing to
    measure — a provable zero, and the state a just-armed soak is in. If there
    *are* observations, sessions were recorded against a journal that is now
    absent, and that is missing evidence.

    Distinguishing them is what lets an armed soak score B while a soak whose
    journal vanished scores C.
    """
    return not context.records.operations_journal().present and not context.observations


def _runs(context: DerivationContext) -> tuple[str, ...]:
    """The run ids in scope, from the context or from the journal."""
    if context.run_ids:
        return context.run_ids
    read = context.records.operations_journal()
    return tuple(dict.fromkeys(str(row.run_id) for row in read.records))


def _events(context: DerivationContext, event_type: str) -> tuple[Any, ...]:
    """Journal entries of one type, restricted to the session's runs."""
    return context.records.journal_events(event_type=event_type, run_ids=_runs(context))


def _by_run(rows: Iterable[Any]) -> dict[str, list[Any]]:
    """Group journal entries by run id."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row.run_id), []).append(row)
    return grouped


def _submitted(context: DerivationContext) -> tuple[Any, ...]:
    """Every order that became real, in this session's runs."""
    return _events(context, "ORDER_SUBMITTED")


# ---------------------------------------------------------------------------
# §26 live mutations
# ---------------------------------------------------------------------------


def _derive_live_mutations(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Count mutations actually sent to a live destination.

    The request audit is the authority, and today nothing in production
    supplies one. That absence is only harmless while nothing was mutated: a
    session that submitted orders with no audit beside them cannot prove where
    they went, so it reports missing evidence rather than zero.

    A *blocked* live attempt — audited with `sent=false` — is not a live
    mutation. It is evidence the host lock worked.
    """
    derivation = build()["audited_live_mutations_v1"]
    audit = context.records.request_audit()
    orders = _submitted(context)
    if not audit.usable:
        if orders:
            return _missing(
                derivation,
                context,
                gate_id,
                f"{len(orders)} order(s) reached a venue in this session and the "
                "external request audit is absent, so no record establishes which "
                "destination they were sent to",
            )
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=(
                "no order reached any venue in this session, so none reached a live "
                "one; established from the operations journal rather than from an "
                "absent audit",
            ),
        )
    live = [
        row
        for row in audit.records
        if str(row.get("destination", "")) == "LIVE" and bool(row.get("sent", False))
    ]
    return derivation.observed(
        context,
        gate_id,
        len(live),
        detail=tuple(f"LIVE mutation sent: {row.get('attempt_id', '?')}" for row in live),
        identities=tuple(str(row.get("attempt_id", "")) for row in live),
    )


# ---------------------------------------------------------------------------
# §12 duplicate remote orders
# ---------------------------------------------------------------------------


def _derive_duplicate_remote_orders(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """One semantic submission authority must yield one remote order.

    The client order id is derived from the local order id, so it *is* the
    semantic authority: two distinct remote orders under one client order id
    means the venue accepted the same authorisation twice. Reading the same
    remote id repeatedly — which recovery does by design — is not a duplicate,
    so distinct ids are counted, not events.
    """
    derivation = build()["duplicate_remote_orders_v1"]
    remote_by_client: dict[str, set[str]] = {}
    for row in _submitted(context):
        evidence = _evidence(row)
        client = evidence.get("client_order_id", "")
        remote = evidence.get("remote_order_id", "")
        if not client or not remote:
            continue
        remote_by_client.setdefault(client, set()).add(remote)
    duplicates = {key: value for key, value in remote_by_client.items() if len(value) > 1}
    excess = sum(len(value) - 1 for value in duplicates.values())
    return derivation.observed(
        context,
        gate_id,
        excess,
        detail=tuple(
            f"{client} reached the venue as {sorted(remotes)}"
            for client, remotes in sorted(duplicates.items())
        ),
        identities=tuple(sorted(duplicates)),
    )


# ---------------------------------------------------------------------------
# §13 duplicate admitted fills
# ---------------------------------------------------------------------------


def _derive_duplicate_admitted_fills(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """One provider execution must be admitted to the book at most once.

    `DUPLICATE_IGNORED` is counted as evidence that duplicate handling worked,
    not as a breach. A breach is the same provider execution admitted twice.
    """
    derivation = build()["duplicate_admitted_fills_v1"]
    admitted: dict[str, int] = {}
    ignored = 0
    for row in _events(context, "FILL_INGESTED"):
        evidence = _evidence(row)
        execution = evidence.get("provider_execution_id", "")
        if not execution:
            continue
        if evidence.get("admission_result") == "ADMITTED":
            admitted[execution] = admitted.get(execution, 0) + 1
        else:
            ignored += 1
    excess = sum(count - 1 for count in admitted.values() if count > 1)
    detail = [
        f"{execution} admitted {count} times"
        for execution, count in sorted(admitted.items())
        if count > 1
    ]
    if ignored:
        detail.append(f"{ignored} duplicate presentation(s) correctly ignored")
    return derivation.observed(
        context, gate_id, excess, detail=tuple(detail), identities=tuple(sorted(admitted))
    )


# ---------------------------------------------------------------------------
# §14 duplicate accounting events
# ---------------------------------------------------------------------------


def _derive_duplicate_accounting_events(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """One admitted fill must become exactly one accounting application.

    Proved along the whole chain rather than by ledger uniqueness alone: the
    ledger enforcing unique event ids says nothing about whether two provider
    executions collapsed into one application, or one execution was applied
    under two ids. So the derivation walks provider execution → execution fill
    → accounting event, and cross-checks both the ledger's committed FILL
    events and the execution journal's FILL_APPLIED events.
    """
    derivation = build()["duplicate_accounting_events_v1"]
    chain: list[tuple[str, str, str]] = []
    for row in _events(context, "FILL_INGESTED"):
        evidence = _evidence(row)
        if evidence.get("admission_result") != "ADMITTED":
            continue
        chain.append(
            (
                evidence.get("provider_execution_id", ""),
                evidence.get("execution_fill_id", ""),
                evidence.get("accounting_event_id", ""),
            )
        )
    problems: list[str] = []

    # One accounting event id may not serve two provider executions.
    by_accounting: dict[str, set[str]] = {}
    for execution, _fill, accounting in chain:
        if accounting:
            by_accounting.setdefault(accounting, set()).add(execution)
    problems += [
        f"accounting event {accounting} serves executions {sorted(executions)}"
        for accounting, executions in sorted(by_accounting.items())
        if len(executions) > 1
    ]

    # One provider execution may not have produced two accounting events.
    by_execution: dict[str, set[str]] = {}
    for execution, _fill, accounting in chain:
        if execution and accounting:
            by_execution.setdefault(execution, set()).add(accounting)
    problems += [
        f"execution {execution} produced accounting events {sorted(events)}"
        for execution, events in sorted(by_execution.items())
        if len(events) > 1
    ]

    # The ledger must hold each accounting event exactly once.
    ledger_counts: dict[str, int] = {}
    for event in context.records.accounting_ledger().records:
        if str(event.get("event_type", "")) == "FILL":
            key = str(event.get("event_id", ""))
            ledger_counts[key] = ledger_counts.get(key, 0) + 1
    problems += [
        f"ledger holds {accounting} {ledger_counts[accounting]} times"
        for accounting in sorted(by_accounting)
        if ledger_counts.get(accounting, 0) > 1
    ]

    # The execution journal must apply each fill exactly once.
    applied: dict[str, int] = {}
    for record in context.records.execution_journal().records:
        event = record.event
        if str(event.event_type) == "FILL_APPLIED":
            key = str(getattr(event, "fill_id", ""))
            applied[key] = applied.get(key, 0) + 1
    problems += [
        f"execution journal applied fill {fill} {applied[fill]} times"
        for fill in sorted({row[1] for row in chain if row[1]})
        if applied.get(fill, 0) > 1
    ]
    return derivation.observed(
        context,
        gate_id,
        len(problems),
        detail=tuple(problems),
        identities=tuple(sorted(by_accounting)),
    )


# ---------------------------------------------------------------------------
# §15 duplicate scheduled runs causing execution
# ---------------------------------------------------------------------------


def _derive_duplicate_scheduled_execution(
    context: DerivationContext, gate_id: str
) -> DerivedMetric:
    """One scheduled semantic event must cause at most one execution authority.

    The operations run id is derived from the slot — schedule, session, account,
    mode and purpose — so repeated recovery of one scheduled event reuses one
    id and is safe by construction. A breach is two *different* run ids both
    submitting orders for the same session.
    """
    derivation = build()["duplicate_scheduled_execution_v1"]
    executing = sorted({str(row.run_id) for row in _submitted(context)})
    excess = max(0, len(executing) - 1)
    return derivation.observed(
        context,
        gate_id,
        excess,
        detail=(
            (f"{len(executing)} distinct run ids submitted orders: {executing}",) if excess else ()
        ),
        identities=tuple(executing),
    )


# ---------------------------------------------------------------------------
# §17, §18 stale and missing input executions
# ---------------------------------------------------------------------------


def _input_breach(context: DerivationContext, gate_id: str, *, counter: str) -> DerivedMetric:
    """Pair an input-freeze verdict with whether that run then mutated."""
    derivation = build()[
        "stale_input_execution_v1" if counter == "stale_count" else "missing_input_execution_v1"
    ]
    frozen = _by_run(_events(context, "INPUTS_FROZEN"))
    submitted = _by_run(_submitted(context))
    breaches: list[str] = []
    unreadable: list[str] = []
    for run_id, orders in sorted(submitted.items()):
        rows = frozen.get(run_id, [])
        if not rows:
            unreadable.append(
                f"run {run_id} submitted {len(orders)} order(s) with no input-freeze record"
            )
            continue
        for row in rows:
            evidence = _evidence(row)
            if evidence.get("inputs") == "UNREADABLE":
                unreadable.append(f"run {run_id} could not read its required inputs")
                continue
            if int(evidence.get(counter, "0") or 0) > 0:
                breaches.append(
                    f"run {run_id} submitted {len(orders)} order(s) with "
                    f"{evidence[counter]} {counter.removesuffix('_count')} input(s)"
                )
    if unreadable:
        return _missing(derivation, context, gate_id, *unreadable)
    return derivation.observed(
        context, gate_id, len(breaches), detail=tuple(breaches), identities=tuple(submitted)
    )


# ---------------------------------------------------------------------------
# §19 missed run backfills
# ---------------------------------------------------------------------------


def _derive_missed_run_backfills(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """A decision whose execution window was missed must never be executed.

    Read from the decision lifecycle rather than from a flag: a record in
    `EXECUTION_MISSED` that also has an execution run id, or whose fingerprint a
    plan later consumed, was backfilled.
    """
    derivation = build()["missed_run_backfill_v1"]
    consumed = {
        _evidence(row).get("decision", "")
        for row in _events(context, "PLAN_AUTHORIZED")
        if _evidence(row).get("decision")
    }
    breaches: list[str] = []
    for body in context.records.decisions().records:
        status = str(body.get("status", ""))
        if status not in {"EXECUTION_MISSED", "SUPERSEDED"}:
            continue
        fingerprint = str(body.get("decision_fingerprint", ""))
        if fingerprint in consumed:
            breaches.append(
                f"decision {fingerprint[:16]}… is {status} and a plan consumed it anyway"
            )
        elif str(body.get("execution_run_id", "")):
            breaches.append(
                f"decision {fingerprint[:16]}… is {status} and names execution run "
                f"{body['execution_run_id']}"
            )
    return derivation.observed(context, gate_id, len(breaches), detail=tuple(breaches))


# ---------------------------------------------------------------------------
# §20, §22 reconciliation breaks and remote-state-unknown
# ---------------------------------------------------------------------------


def _reconciliation_breach(context: DerivationContext, gate_id: str, *, key: str) -> DerivedMetric:
    """Pair a reconciliation verdict with whether that run then mutated.

    A break that blocked execution is the system working. A break followed by a
    mutation in the same run is the breach.
    """
    derivation = build()[
        "ignored_reconciliation_break_v1" if key == "may_execute" else "ignored_remote_unknown_v1"
    ]
    reconciled = _by_run(_events(context, "RECONCILED"))
    submitted = _by_run(_submitted(context))
    breaches: list[str] = []
    missing: list[str] = []
    for run_id, orders in sorted(submitted.items()):
        rows = reconciled.get(run_id, [])
        if not rows:
            missing.append(
                f"run {run_id} submitted {len(orders)} order(s) with no reconciliation record"
            )
            continue
        for row in rows:
            evidence = _evidence(row)
            if key not in evidence:
                missing.append(f"run {run_id} reconciliation record does not state {key}")
                continue
            forbidden = (
                evidence[key] == "False" if key == "may_execute" else evidence[key] == "True"
            )
            if forbidden:
                breaches.append(
                    f"run {run_id} submitted {len(orders)} order(s) after {key}={evidence[key]}"
                )
    if missing:
        return _missing(derivation, context, gate_id, *missing)
    return derivation.observed(
        context, gate_id, len(breaches), detail=tuple(breaches), identities=tuple(submitted)
    )


# ---------------------------------------------------------------------------
# §21 completed sessions reconcile
# ---------------------------------------------------------------------------


def _derive_completed_sessions_reconcile(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """A completed run that traded must carry its own final reconciliation.

    Counted as *not clean* when the evidence is absent, and never reconstructed
    from today's broker state: asking the venue now what was true then is not
    reconciliation, it is a guess with a timestamp.
    """
    derivation = build()["completed_session_reconciliation_v1"]
    completed = {str(row.run_id) for row in _events(context, "RUN_COMPLETED")}
    submitted = _by_run(_submitted(context))
    reconciled = _by_run(_events(context, "RECONCILED"))
    persisted = set(context.records.reconciliations().identities)
    unclean: list[str] = []
    for run_id in sorted(completed & set(submitted)):
        if run_id not in reconciled:
            unclean.append(f"run {run_id} completed with orders and no reconciliation event")
        elif run_id not in persisted:
            unclean.append(f"run {run_id} completed with no persisted reconciliation report")
    return derivation.observed(
        context, gate_id, len(unclean), detail=tuple(unclean), identities=tuple(sorted(completed))
    )


# ---------------------------------------------------------------------------
# §23 kill bypasses
# ---------------------------------------------------------------------------


def _derive_kill_bypasses(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Every mutation must be preceded by its own permitting gate check.

    Paired by journal sequence rather than by order id, because the gate-check
    evidence does not expose one and this phase does not change runtime
    emitters to make derivation easier. The journal is sequence-ordered and the
    authority emits the check immediately before the order, so each
    ORDER_SUBMITTED must be able to claim an earlier permitted check in the same
    run that no earlier order has already claimed. A mutation with no
    unclaimed check before it is a mutation nothing permitted.
    """
    derivation = build()["per_mutation_gate_check_v1"]
    read = context.records.operations_journal()
    runs = set(_runs(context))
    breaches: list[str] = []
    permitted_by_run: dict[str, int] = {}
    for row in sorted(read.records, key=lambda entry: int(entry.sequence)):
        run_id = str(row.run_id)
        if run_id not in runs:
            continue
        kind = str(row.event_type)
        if kind == "SUBMISSION_GATE_CHECKED":
            if _evidence(row).get("permitted") == "True":
                permitted_by_run[run_id] = permitted_by_run.get(run_id, 0) + 1
        elif kind == "ORDER_SUBMITTED":
            available = permitted_by_run.get(run_id, 0)
            if available <= 0:
                breaches.append(
                    f"run {run_id} sequence {row.sequence}: an order reached the venue "
                    "with no unclaimed permitting gate check before it"
                )
            else:
                permitted_by_run[run_id] = available - 1
    return derivation.observed(context, gate_id, len(breaches), detail=tuple(breaches))


# ---------------------------------------------------------------------------
# §27 unknown remote order auto-cancels
# ---------------------------------------------------------------------------


def _derive_unknown_order_auto_cancels(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Nothing may cancel an order this runtime does not own.

    Two independent sources must agree that nothing was cancelled: the
    execution journal holds no cancellation, and the audit holds no DELETE. An
    AlphaTrader-owned cancellation is not automatically a breach; a cancellation
    of an order with no local submission intent is.
    """
    derivation = build()["unknown_order_auto_cancel_v1"]
    owned = set(context.records.submission_intents().identities)
    breaches: list[str] = []

    for record in context.records.execution_journal().records:
        event = record.event
        if str(event.event_type) != "ORDER_CANCELLED":
            continue
        order_id = str(getattr(event, "order_id", ""))
        breaches.append(f"execution journal cancelled {order_id}")

    audit = context.records.request_audit()
    if audit.usable:
        for row in audit.records:
            if str(row.get("method", "")) != "DELETE" or not bool(row.get("sent", False)):
                continue
            client = str(row.get("client_order_id", ""))
            if client and client in owned:
                continue
            breaches.append(
                f"a DELETE was sent for {client or 'an order with no client id'}, "
                "which this runtime has no submission intent for"
            )
    return derivation.observed(
        context, gate_id, len(breaches), detail=tuple(breaches), identities=tuple(sorted(owned))
    )


# ---------------------------------------------------------------------------
# §28 automatic accounting repairs
# ---------------------------------------------------------------------------


def _derive_automatic_accounting_repairs(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """The book may only move through an opening balance and admitted fills.

    An explicit bootstrapped `OPENING_BALANCE` is how a book starts and is not a
    repair. A second one, or any other event type, is the book being written
    from somewhere other than execution — which is the thing this gate exists
    to catch, because overwriting local accounting with broker state destroys
    the only independent record of what was authorised.
    """
    derivation = build()["automatic_accounting_repair_v1"]
    events = context.records.accounting_ledger().records
    openings = [row for row in events if str(row.get("event_type", "")) == "OPENING_BALANCE"]
    others = [
        str(row.get("event_type", ""))
        for row in events
        if str(row.get("event_type", "")) not in {"OPENING_BALANCE", "FILL"}
    ]
    breaches: list[str] = []
    if len(openings) > 1:
        breaches.append(f"the ledger holds {len(openings)} opening balances")
    breaches += [
        f"the ledger holds a {kind} event, which execution never writes" for kind in others
    ]
    return derivation.observed(context, gate_id, len(breaches), detail=tuple(breaches))


# ---------------------------------------------------------------------------
# §24 reranked executions
# ---------------------------------------------------------------------------


def _derive_reranked_executions(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """An execution must consume the decision that was frozen, not a fresh one.

    The plan records which decision fingerprint it consumed; the decision store
    records which fingerprint was frozen. A plan naming a fingerprint no
    persisted decision carries executed something ranked after the close.
    """
    derivation = build()["reranked_execution_v1"]
    persisted = {
        str(body.get("decision_fingerprint", ""))
        for body in context.records.decisions().records
        if body.get("decision_fingerprint")
    }
    breaches: list[str] = []
    for row in _events(context, "PLAN_AUTHORIZED"):
        consumed = _evidence(row).get("decision", "")
        if not consumed:
            breaches.append(f"a plan in run {row.run_id} names no decision")
        elif consumed not in persisted:
            breaches.append(
                f"run {row.run_id} executed decision {consumed[:16]}…, which no "
                "persisted decision carries"
            )
    return derivation.observed(context, gate_id, len(breaches), detail=tuple(breaches))


# ---------------------------------------------------------------------------
# §25 risk authority expansions
# ---------------------------------------------------------------------------


def _derive_risk_authority_expansions(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Risk may reduce the strategy's request and may never expand it.

    Read from the recorded desired-to-approved transformation, not from a
    boolean somebody set: positive exposure to an identity the strategy never
    requested, or above what it requested, is the breach.
    """
    derivation = build()["risk_authority_expansion_v1"]
    breaches: list[str] = []
    for body in context.records.transformations().records:
        session = str(body.get("decision_session", ""))
        desired = {str(name) for name in body.get("desired_identities", ())}
        for name in body.get("approved_identities", ()):
            if str(name) not in desired:
                breaches.append(
                    f"{session}: risk approved {name}, which the strategy never requested"
                )
        for row in body.get("rows", ()):
            approved = float(row.get("approved_weight", 0.0) or 0.0)
            wanted = float(row.get("desired_weight", 0.0) or 0.0)
            if approved > wanted + _WEIGHT_TOLERANCE:
                breaches.append(
                    f"{session}: risk approved {row.get('identity')} at {approved}, "
                    f"above the requested {wanted}"
                )
    return derivation.observed(context, gate_id, len(breaches), detail=tuple(breaches))


# ---------------------------------------------------------------------------
# §16 duplicate counted sessions — SOAK scope
# ---------------------------------------------------------------------------


def _live_observations(context: DerivationContext) -> tuple[Any, ...]:
    """Observations that nothing supersedes."""
    return tuple(row for row in context.observations if not getattr(row, "superseded_by", ""))


def _derive_duplicate_counted_sessions(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """One eligible session may be counted at most once.

    Soak-scoped by nature: a gate about counting a session twice cannot be
    answered from inside one session. Derived from the observation set and its
    supersession graph, never from a counter a writer maintained.
    """
    derivation = build()["duplicate_counted_sessions_v1"]
    counts: dict[str, int] = {}
    for row in _live_observations(context):
        session = str(getattr(row, "session", ""))
        counts[session] = counts.get(session, 0) + 1
    excess = sum(count - 1 for count in counts.values() if count > 1)
    return derivation.observed(
        context,
        gate_id,
        excess,
        detail=tuple(
            f"session {session} is counted {count} times"
            for session, count in sorted(counts.items())
            if count > 1
        ),
    )


# ---------------------------------------------------------------------------
# §44, promotion counts — SOAK scope
# ---------------------------------------------------------------------------


def _derive_qualifying_sessions(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """How many eligible sessions have a live observation.

    Counted from the activation's eligible set and the observations, so a
    writer cannot report progress it did not make.
    """
    derivation = build()["qualifying_sessions_observed_v1"]
    eligible = set(context.eligible_sessions)
    observed = {
        str(getattr(row, "session", ""))
        for row in _live_observations(context)
        if str(getattr(row, "session", "")) in eligible
    }
    return derivation.observed(context, gate_id, len(observed), identities=tuple(sorted(observed)))


def _derive_sessions_with_no_record(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Eligible sessions that have elapsed with nothing recorded."""
    derivation = build()["eligible_sessions_with_no_record_v1"]
    observed = {str(getattr(row, "session", "")) for row in _live_observations(context)}
    missing = sorted(set(context.eligible_sessions) - observed)
    return derivation.observed(
        context, gate_id, len(missing), detail=tuple(missing), identities=tuple(missing)
    )


def _incident_metric(context: DerivationContext, gate_id: str, *, severity: str) -> DerivedMetric:
    """Open incidents of one severity.

    An absent incident directory is only a zero when nothing ever failed a
    health gate. If a run was blocked and no incident was kept, the history is
    missing rather than empty.
    """
    derivation = build()[
        "unresolved_critical_incidents_v1"
        if severity == "CRITICAL"
        else "unresolved_blocking_incidents_v1"
    ]
    if _not_started(context):
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=("the soak has not started: no run has been recorded at all",),
        )
    journal = context.records.operations_journal()
    if not journal.usable:
        return _missing(
            derivation,
            context,
            gate_id,
            f"sessions are recorded and the operations journal {journal.detail}, so "
            "whether a run was blocked and an incident opened cannot be established",
        )
    read = context.records.incidents()
    if not read.usable:
        blocked = [
            row
            for row in context.records.operations_journal().records
            if str(row.to_state) in {"BLOCKED", "KILLED"}
        ]
        if blocked:
            return _missing(
                derivation,
                context,
                gate_id,
                f"{len(blocked)} run(s) ended BLOCKED or KILLED and no incident history "
                "exists, so what was opened and whether it was resolved is unknown",
            )
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=("no run was ever blocked or killed, so no incident was opened",),
        )
    open_rows = [
        row
        for row in read.records
        if str(row.get("severity", "")) == severity
        and str(row.get("status", "")) not in {"RESOLVED", "CLOSED"}
    ]
    return derivation.observed(
        context,
        gate_id,
        len(open_rows),
        detail=tuple(f"{row.get('reason_code')} is {row.get('status')}" for row in open_rows),
        identities=tuple(str(row.get("incident_id", "")) for row in open_rows),
    )


def _derive_decision_cycles(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Decision cycles that completed: decided, then executed and accounted for.

    Read from the decision lifecycle and the execution evidence that followed
    it, so a cycle counts only when both halves are on record.
    """
    derivation = build()["successful_decision_cycles_v1"]
    if _not_started(context):
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=("the soak has not started: no decision has been recorded at all",),
        )
    consumed = {
        _evidence(row).get("decision", "")
        for row in context.records.journal_events(event_type="PLAN_AUTHORIZED")
    }
    complete: list[str] = []
    for body in context.records.decisions().records:
        if str(body.get("status", "")) != "EXECUTION_COMPLETED":
            continue
        fingerprint = str(body.get("decision_fingerprint", ""))
        if fingerprint in consumed:
            complete.append(fingerprint)
    return derivation.observed(context, gate_id, len(complete), identities=tuple(sorted(complete)))


def _derive_unaccounted_executions(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """Decisions still outstanding at completion.

    A decision that is neither completed nor terminally missed is a loose end:
    the soak cannot be declared finished while one is still mid-flight.
    """
    derivation = build()["unaccounted_decision_executions_v1"]
    if _not_started(context):
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=("the soak has not started: no decision is outstanding",),
        )
    settled = {"EXECUTION_COMPLETED", "EXECUTION_MISSED", "SUPERSEDED"}
    pending = [
        str(body.get("decision_fingerprint", ""))
        for body in context.records.decisions().records
        if str(body.get("status", "")) not in settled
    ]
    return derivation.observed(
        context,
        gate_id,
        len(pending),
        detail=tuple(f"decision {row[:16]}… is not settled" for row in pending),
        identities=tuple(pending),
    )


# ---------------------------------------------------------------------------
# §43 required drills — DRILL scope
# ---------------------------------------------------------------------------


def _derive_required_drills(context: DerivationContext, gate_id: str) -> DerivedMetric:
    """How many required drill types have admissible evidence.

    Admissibility is the drill store's own rule, bound to the protocol and the
    build. Evidence for another commit does not certify this one, which is why
    the count is of admissible records rather than of files.
    """
    from .drills_v2 import DrillRecordV2, admissible_drills

    derivation = build()["required_drills_passed_v1"]
    read = context.records.drills()
    if not read.present:
        # An absent drill store is a legitimate zero, and this is the one place
        # that is true. `required_drills_passed` is a floor: zero means "the
        # drills have not been run yet", which keeps the verdict at B and is
        # exactly what an armed soak looks like before anybody runs them.
        #
        # The rule is about direction, not about this gate. For an AT_MOST
        # safety gate, absence must never read as clean — "no record of a kill
        # bypass" is not "no kill bypass". For an AT_LEAST promotion gate,
        # absence of accumulated evidence *is* the observation that it has not
        # accumulated, and reporting it as unmeasurable would turn every
        # just-armed soak into an integrity failure.
        return derivation.observed(
            context,
            gate_id,
            0,
            detail=("no drill evidence has been recorded yet",),
        )
    if not read.verified:
        return _missing(derivation, context, gate_id, f"drill evidence {read.detail}")
    records: list[DrillRecordV2] = []
    for body in read.records:
        try:
            records.append(_drill_from(body))
        except Exception as exc:
            # An unreadable drill record is not a passed drill.
            return _missing(
                derivation,
                context,
                gate_id,
                f"a drill record could not be read: {type(exc).__name__}: {exc}",
            )
    admitted, rejected = admissible_drills(
        tuple(records),
        protocol_fingerprint=context.protocol_fingerprint,
        qualifying_commit=context.qualifying_commit,
        operations_engine_version=_operations_engine_version(),
        operations_policy_fingerprint=context.operations_policy_fingerprint,
    )
    passed = {
        str(row.drill_type)
        for row in admitted
        if row.passed and str(row.drill_type) in set(context.required_drills)
    }
    return derivation.observed(
        context,
        gate_id,
        len(passed),
        detail=tuple(rejected),
        identities=tuple(sorted(passed)),
    )


def _drill_from(body: dict[str, Any]) -> Any:
    """Rebuild a drill record from its stored body."""
    from .drills_v2 import _from_body

    return _from_body(body)


def _operations_engine_version() -> str:
    """The control-plane version drills must have been recorded under."""
    from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION

    return str(PAPER_OPERATIONS_ENGINE_VERSION)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Derivation] | None = None


def build() -> dict[str, Derivation]:
    """Every derivation, by id. Built once.

    Returns:
        The registry.
    """
    # One immutable registry, built on demand.
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    rows = [
        Derivation(
            derivation_id="audited_live_mutations_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("live_mutations",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule=(
                "count mutations the request audit records as sent to a LIVE "
                "destination; a session that mutated with no audit reports missing "
                "evidence rather than zero"
            ),
            derive=_derive_live_mutations,
        ),
        Derivation(
            derivation_id="duplicate_remote_orders_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("duplicate_remote_orders",),
            required_schemas=("OPERATIONS_RUN_JOURNAL", "SUBMISSION_INTENTS"),
            rule="one derived client order id must map to one remote order",
            derive=_derive_duplicate_remote_orders,
        ),
        Derivation(
            derivation_id="duplicate_admitted_fills_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("duplicate_admitted_fills",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="one provider execution must be ADMITTED at most once",
            derive=_derive_duplicate_admitted_fills,
        ),
        Derivation(
            derivation_id="duplicate_accounting_events_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("duplicate_accounting_events",),
            required_schemas=(
                "OPERATIONS_RUN_JOURNAL",
                "ACCOUNTING_LEDGER",
                "EXECUTION_JOURNAL",
            ),
            rule=(
                "provider execution to execution fill to accounting event must be "
                "one to one along the whole chain"
            ),
            derive=_derive_duplicate_accounting_events,
        ),
        Derivation(
            derivation_id="duplicate_scheduled_execution_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("duplicate_scheduled_runs_causing_execution",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="one scheduled semantic event must cause at most one submitting run",
            derive=_derive_duplicate_scheduled_execution,
        ),
        Derivation(
            derivation_id="stale_input_execution_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("stale_data_executions",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="a run with a stale required input must not have submitted an order",
            derive=lambda context, gate_id: _input_breach(context, gate_id, counter="stale_count"),
        ),
        Derivation(
            derivation_id="missing_input_execution_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("missing_data_executions",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="a run with a missing required input must not have submitted an order",
            derive=lambda context, gate_id: _input_breach(
                context, gate_id, counter="missing_count"
            ),
        ),
        Derivation(
            derivation_id="missed_run_backfill_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("missed_run_backfills",),
            required_schemas=("OPERATIONS_RUN_JOURNAL", "STRATEGY_DECISIONS"),
            rule="a missed or superseded decision must never be executed later",
            derive=_derive_missed_run_backfills,
        ),
        Derivation(
            derivation_id="ignored_reconciliation_break_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("ignored_reconciliation_breaks",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="a run told it may not execute must not have submitted an order",
            derive=lambda context, gate_id: _reconciliation_breach(
                context, gate_id, key="may_execute"
            ),
        ),
        Derivation(
            derivation_id="ignored_remote_unknown_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("ignored_remote_state_unknown",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="a run whose remote state was unknown must not have submitted an order",
            derive=lambda context, gate_id: _reconciliation_breach(
                context, gate_id, key="remote_state_unknown"
            ),
        ),
        Derivation(
            derivation_id="completed_session_reconciliation_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("completed_sessions_reconcile",),
            required_schemas=("OPERATIONS_RUN_JOURNAL", "RECONCILIATION_RECORDS"),
            rule="a completed run that traded must carry its own final reconciliation",
            derive=_derive_completed_sessions_reconcile,
        ),
        Derivation(
            derivation_id="per_mutation_gate_check_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("kill_bypasses",),
            required_schemas=("OPERATIONS_RUN_JOURNAL",),
            rule="every mutation must be preceded by its own permitting gate check",
            derive=_derive_kill_bypasses,
        ),
        Derivation(
            derivation_id="unknown_order_auto_cancel_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("unknown_remote_order_auto_cancels",),
            required_schemas=("EXECUTION_JOURNAL", "SUBMISSION_INTENTS"),
            rule="nothing may cancel an order this runtime has no intent for",
            derive=_derive_unknown_order_auto_cancels,
        ),
        Derivation(
            derivation_id="automatic_accounting_repair_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("automatic_accounting_repairs",),
            required_schemas=("ACCOUNTING_LEDGER",),
            rule="the book moves only through one opening balance and admitted fills",
            derive=_derive_automatic_accounting_repairs,
        ),
        Derivation(
            derivation_id="reranked_execution_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("reranked_executions",),
            required_schemas=("OPERATIONS_RUN_JOURNAL", "STRATEGY_DECISIONS"),
            rule="an execution must consume a persisted decision, never a fresh ranking",
            derive=_derive_reranked_executions,
        ),
        Derivation(
            derivation_id="risk_authority_expansion_v1",
            version="V1",
            scope=GateScope.SESSION,
            gates=("risk_authority_expansions",),
            required_schemas=("RISK_TRANSFORMATIONS",),
            rule="risk may reduce the strategy's request and may never expand it",
            derive=_derive_risk_authority_expansions,
        ),
        Derivation(
            derivation_id="duplicate_counted_sessions_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("duplicate_counted_sessions",),
            required_schemas=(),
            rule="one eligible session may be counted at most once",
            derive=_derive_duplicate_counted_sessions,
        ),
        Derivation(
            derivation_id="qualifying_sessions_observed_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("qualifying_sessions_observed",),
            required_schemas=(),
            rule="count eligible sessions with a live observation",
            derive=_derive_qualifying_sessions,
        ),
        Derivation(
            derivation_id="eligible_sessions_with_no_record_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("eligible_sessions_with_no_record",),
            required_schemas=(),
            rule="count eligible sessions with nothing recorded",
            derive=_derive_sessions_with_no_record,
        ),
        Derivation(
            derivation_id="unresolved_critical_incidents_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("unresolved_critical_incidents",),
            required_schemas=(),
            rule="count open CRITICAL incidents",
            derive=lambda context, gate_id: _incident_metric(context, gate_id, severity="CRITICAL"),
        ),
        Derivation(
            derivation_id="unresolved_blocking_incidents_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("unresolved_blocking_incidents",),
            required_schemas=(),
            rule="count open BLOCKING incidents",
            derive=lambda context, gate_id: _incident_metric(context, gate_id, severity="BLOCKING"),
        ),
        Derivation(
            derivation_id="successful_decision_cycles_v1",
            version="V1",
            scope=GateScope.SOAK,
            gates=("successful_strategy_decision_cycles",),
            required_schemas=(),
            rule="count decisions that were executed and accounted for",
            derive=_derive_decision_cycles,
        ),
        Derivation(
            derivation_id="required_drills_passed_v1",
            version="V1",
            scope=GateScope.DRILL,
            gates=("required_drills_passed",),
            required_schemas=(),
            rule="count required drill types with admissible passing evidence",
            derive=_derive_required_drills,
        ),
        Derivation(
            derivation_id="unaccounted_decision_executions_v1",
            version="V1",
            scope=GateScope.COMPLETION,
            gates=("eligible_decision_executions_accounted_for",),
            required_schemas=(),
            rule="count decisions still unsettled at completion",
            derive=_derive_unaccounted_executions,
        ),
    ]
    _REGISTRY = {row.derivation_id: row for row in rows}
    return _REGISTRY
