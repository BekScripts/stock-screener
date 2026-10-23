"""The record of one external paper run.

A report is what someone reads a week later to answer "what did this do". For an
external run the question has an extra edge, because the actions were real: an
order existed at a venue, and some of them may still.

So the report is built around what is *known*, and says where knowledge stops.
It names every submission and its outcome, including the ones that ended
`REMOTE_STATE_UNKNOWN` — those are listed first in their own field rather than
summarised into a count, because an unknown order is the single most actionable
thing a run can produce and a reader should not have to go looking for it.

It carries no credential, no account number, no authorization header and no key
of any kind. `check_no_credentials` is not decoration: it is a test seam that
scans the serialised report for anything shaped like a secret, so the claim is
checked rather than asserted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from ..fingerprint import fingerprint_of
from .submission import SubmissionOutcome

if TYPE_CHECKING:
    from .preflight import PaperBrokerReadiness
    from .reconciliation import ExternalReconciliationResult
    from .submission import SubmissionResult

__all__ = [
    "CREDENTIAL_PATTERNS",
    "ExternalPaperExecutionReport",
    "check_no_credentials",
]

#: What a credential tends to look like. Used to check a report rather than to
#: sanitise one — sanitising a report that should never have held a secret would
#: hide the bug that put it there.
CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an Alpaca live key id", re.compile(r"\bAK[A-Z0-9]{10,}\b")),
    ("an Alpaca paper key id", re.compile(r"\bPK[A-Z0-9]{10,}\b")),
    ("an authorization header", re.compile(r"(?i)\bauthorization\b\s*[:=]")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")),
    ("an API secret field", re.compile(r"(?i)\b(api[_-]?secret|secret[_-]?key)\b")),
    ("an Alpaca secret header", re.compile(r"(?i)\bAPCA-API-SECRET-KEY\b")),
    ("a cookie header", re.compile(r"(?i)\bset-cookie\b")),
)


def check_no_credentials(payload: Any, *, context: str = "a report") -> None:
    """Refuse a payload that carries anything shaped like a secret.

    Args:
        payload: Anything JSON-serialisable.
        context: What is being checked, for the message.

    Raises:
        ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` naming the pattern that
            matched. The payload itself is never included in the message — a
            refusal that quoted the secret would be the leak it exists to
            prevent.
    """
    text = json.dumps(payload, default=str)
    for description, pattern in CREDENTIAL_PATTERNS:
        if pattern.search(text):
            raise ExecutionError(
                Code.FORBIDDEN_EXECUTION_NAMESPACE,
                f"{context} contains something shaped like {description}. It is "
                "refused, and the matching text is deliberately not quoted here",
            )


@dataclass(frozen=True)
class ExternalPaperExecutionReport:
    """What one external paper run did, and what it does not know.

    Attributes:
        manifest: The V1.2 manifest.
        readiness: What preflight concluded.
        submissions: Every submission, in submission order.
        reconciliation: What the three-way sweep concluded, when one ran.
        notes: Anything a reader needs that the figures do not say.
    """

    manifest: dict[str, Any]
    readiness: PaperBrokerReadiness
    submissions: tuple[SubmissionResult, ...] = ()
    reconciliation: ExternalReconciliationResult | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unknown_submissions(self) -> tuple[SubmissionResult, ...]:
        """Every submission whose fate is not established.

        The most actionable thing a run can produce: each one may be a working
        order at the venue that the book does not know about.
        """
        return tuple(
            row for row in self.submissions if row.outcome is SubmissionOutcome.REMOTE_STATE_UNKNOWN
        )

    @property
    def confirmed_submissions(self) -> tuple[SubmissionResult, ...]:
        """Every submission the venue confirmed."""
        return tuple(
            row for row in self.submissions if row.outcome is SubmissionOutcome.REMOTE_CONFIRMED
        )

    @property
    def total_mutating_requests(self) -> int:
        """How many mutating requests the run made in total.

        Equal to the number of submission attempts, always. A larger number
        would mean something retried.
        """
        return sum(row.mutating_requests for row in self.submissions)

    @property
    def is_clean(self) -> bool:
        """Whether the run ended with nothing outstanding.

        False if any submission is unknown or any reconciliation finding is
        blocking. Deliberately not true merely because every order filled.
        """
        if self.unknown_submissions:
            return False
        if self.reconciliation is not None and not self.reconciliation.may_execute:
            return False
        return self.readiness.is_ready

    def body(self) -> dict[str, Any]:
        """Return the report's semantic content."""
        return {
            "manifest_fingerprint": self.manifest.get("manifest_fingerprint"),
            "manifest_version": self.manifest.get("manifest_version"),
            "venue": self.manifest.get("venue", {}).get("venue"),
            "readiness": self.readiness.as_json(),
            "submissions": [row.as_json() for row in self.submissions],
            "unknown_submissions": [row.intent.order_id for row in self.unknown_submissions],
            "reconciliation": (
                self.reconciliation.as_json() if self.reconciliation is not None else None
            ),
            "total_mutating_requests": self.total_mutating_requests,
            "is_clean": self.is_clean,
            "notes": list(self.notes),
            "live_trading": "NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the report."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the report, checked for credentials before it is handed out.

        Returns:
            The report, with its own fingerprint.

        Raises:
            ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` if anything in it is
                shaped like a secret. Checked on the way out, so a report cannot
                be written, logged or printed without passing.
        """
        body = {
            **self.body(),
            "manifest": self.manifest,
            "report_fingerprint": self.fingerprint(),
        }
        check_no_credentials(body, context="an external paper execution report")
        return body

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report as lines a person can read in a terminal.

        Returns:
            The lines. The unknown submissions come first when there are any,
            because that is what a reader needs to act on.
        """
        lines = [
            f"venue: {self.manifest.get('venue', {}).get('venue')}",
            f"readiness: {self.readiness.verdict}",
            f"submissions: {len(self.submissions)} ({len(self.confirmed_submissions)} confirmed)",
            f"mutating requests: {self.total_mutating_requests}",
        ]
        if self.unknown_submissions:
            lines.insert(
                0,
                f"ATTENTION: {len(self.unknown_submissions)} submission(s) in an "
                "unknown state — each may be a working order the book does not "
                f"know about: {[row.intent.order_id for row in self.unknown_submissions]}",
            )
        if self.reconciliation is not None:
            lines.append(
                f"reconciliation: {self.reconciliation.verdict} "
                f"({len(self.reconciliation.blocking)} blocking)"
            )
        lines.append("live trading: NOT_AUTHORIZED")
        return tuple(lines)
