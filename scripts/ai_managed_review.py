#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Translate managed Codex GitHub review artifacts into the local review schema.

The managed ChatGPT Codex connector posts ordinary GitHub reviews and comments
instead of the JSON emitted by `openai/codex-action`. This adapter is deliberately
network-free: the workflow fetches GitHub API payloads, then this script classifies
only artifacts authored by the official connector and bound to the exact PR head.

Usage:
    uv run scripts/ai_managed_review.py \
        --head-sha <sha> \
        --reviews reviews.json \
        --issue-comments issue-comments.json \
        --review-comments review-comments.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import cast

BOT_LOGIN = "chatgpt-codex-connector[bot]"
CLEAN_PREFIX = "Codex Review: Didn't find any major issues."
SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
SUMMARY_COMPLETED = "✅ **Completed**"
REVIEWED_SHA_RE = re.compile(
    r"\*\*Reviewed commit:\*\*\s*`(?P<sha>[0-9a-fA-F]{7,40})`"
)
SUMMARY_SHA_RE = re.compile(r"\|\s*`(?P<sha>[0-9a-fA-F]{7,40})`\s*\|")
BADGE_RE = re.compile(r"!\[P[0-3] Badge\]\([^)]+\)")
HTML_RE = re.compile(r"<[^>]+>")
PRIORITY_RE = re.compile(r"\bP(?P<priority>[0-3])\b")


class ManagedReviewError(RuntimeError):
    """Raised when a managed-review payload is malformed."""


def _load_array(path: Path) -> list[dict[str, object]]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ManagedReviewError(f"{path} must contain a JSON array")

    items: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ManagedReviewError(f"{path} contains a non-object item")
        items.append(cast("dict[str, object]", item))
    return items


def _text(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _integer(item: dict[str, object], key: str) -> int | None:
    value = item.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_codex(item: dict[str, object]) -> bool:
    raw_user = item.get("user")
    if not isinstance(raw_user, dict):
        return False
    user = cast("dict[str, object]", raw_user)
    return _text(user, "login") == BOT_LOGIN


def _stamp(item: dict[str, object]) -> str:
    for key in ("submitted_at", "created_at", "updated_at"):
        value = _text(item, key)
        if value:
            return value
    identifier = _integer(item, "id")
    return f"id:{identifier or 0:020d}"


def _extract_reviewed_sha(body: str) -> str:
    match = REVIEWED_SHA_RE.search(body)
    return match.group("sha").lower() if match is not None else ""


def _head_matches(candidate: str, head_sha: str) -> bool:
    normalized = candidate.strip().lower()
    head = head_sha.strip().lower()
    return len(normalized) >= 7 and head.startswith(normalized)


def _review_matches_head(review: dict[str, object], head_sha: str) -> bool:
    commit_id = _text(review, "commit_id").lower()
    if commit_id:
        return commit_id == head_sha.lower()
    return _head_matches(_extract_reviewed_sha(_text(review, "body")), head_sha)


def _clean_comment_matches_head(comment: dict[str, object], head_sha: str) -> bool:
    body = _text(comment, "body")
    reviewed_sha = _extract_reviewed_sha(body)
    return body.startswith(CLEAN_PREFIX) and _head_matches(reviewed_sha, head_sha)


def _summary_matches_head(comment: dict[str, object], head_sha: str) -> bool:
    body = _text(comment, "body")
    if SUMMARY_MARKER not in body:
        return False

    for line in body.splitlines():
        if "**Code Review**" not in line or SUMMARY_COMPLETED not in line:
            continue
        match = SUMMARY_SHA_RE.search(line)
        if match is not None and _head_matches(match.group("sha"), head_sha):
            return True
    return False


def _severity(body: str) -> str:
    match = PRIORITY_RE.search(body)
    if match is None:
        return "HIGH"
    return {
        "0": "BLOCKER",
        "1": "HIGH",
        "2": "MEDIUM",
        "3": "LOW",
    }[match.group("priority")]


def _title(body: str) -> str:
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    first = BADGE_RE.sub("", first)
    first = HTML_RE.sub("", first)
    first = first.replace("**", "").replace("__", "").strip()
    for token in ("[CHANGES_REQUESTED]", "[HUMAN_REQUIRED]", "[ARCHITECTURE_BLOCKED]"):
        first = first.replace(token, "").strip()
    return first[:240] or "Managed Codex finding"


def _verdict(bodies: list[str]) -> str:
    joined = "\n".join(bodies)
    if "HUMAN_REQUIRED" in joined:
        return "HUMAN_REQUIRED"
    if "ARCHITECTURE_BLOCKED" in joined:
        return "ARCHITECTURE_BLOCKED"
    return "CHANGES_REQUESTED"


def _finding(comment: dict[str, object]) -> dict[str, object]:
    body = _text(comment, "body").strip()
    verdict = _verdict([body])
    line = _integer(comment, "line") or _integer(comment, "original_line")
    path = _text(comment, "path") or None

    if verdict == "HUMAN_REQUIRED":
        rule = ".ai/REVIEW_CONTRACT.md human-only boundary"
        required_fix = "Stop for explicit human/operator approval; do not auto-fix this finding."
    elif verdict == "ARCHITECTURE_BLOCKED":
        rule = ".ai/REVIEW_CONTRACT.md architecture boundary"
        required_fix = (
            "Stop for an explicit architecture/authority decision; "
            "do not auto-fix this finding."
        )
    else:
        rule = "Managed Codex Code Review finding"
        required_fix = (
            "Resolve the defect described by managed Codex without broadening scope, "
            "and add or adjust a regression test when applicable."
        )

    return {
        "severity": _severity(body),
        "file": path,
        "line": line,
        "rule": rule,
        "title": _title(body),
        "details": body,
        "required_fix": required_fix,
    }


def _review_decision(
    head_sha: str,
    reviews: list[dict[str, object]],
    review_comments: list[dict[str, object]],
) -> tuple[str, dict[str, object]] | None:
    matching = [
        review
        for review in reviews
        if _is_codex(review)
        and _text(review, "state").upper() != "PENDING"
        and _review_matches_head(review, head_sha)
    ]
    if not matching:
        return None

    review = max(matching, key=_stamp)
    review_id = _integer(review, "id")
    if review_id is None:
        raise ManagedReviewError("matching Codex review has no integer id")

    comments = [
        comment
        for comment in review_comments
        if _is_codex(comment) and _integer(comment, "pull_request_review_id") == review_id
    ]
    if not comments:
        body = _text(review, "body")
        if body.startswith(CLEAN_PREFIX):
            return (
                _stamp(review),
                {
                    "verdict": "APPROVE",
                    "summary": f"Managed Codex found no material issues on {head_sha[:12]}.",
                    "findings": [],
                },
            )
        return None

    review_body = _text(review, "body")
    comment_bodies = [_text(comment, "body") for comment in comments]
    verdict = _verdict([review_body, *comment_bodies])
    findings = [_finding(comment) for comment in comments]
    if verdict in {"HUMAN_REQUIRED", "ARCHITECTURE_BLOCKED"} and verdict in review_body:
        findings.insert(0, _finding({"body": review_body}))
    return (
        _stamp(review),
        {
            "verdict": verdict,
            "summary": (
                f"Managed Codex reported {len(findings)} finding(s) on {head_sha[:12]} "
                f"with verdict {verdict}."
            ),
            "findings": findings,
        },
    )


def _comment_decision(
    head_sha: str,
    comments: list[dict[str, object]],
) -> tuple[str, dict[str, object]] | None:
    clean = [
        comment
        for comment in comments
        if _is_codex(comment) and _clean_comment_matches_head(comment, head_sha)
    ]
    if not clean:
        return None
    comment = max(clean, key=_stamp)
    return (
        _stamp(comment),
        {
            "verdict": "APPROVE",
            "summary": f"Managed Codex found no material issues on {head_sha[:12]}.",
            "findings": [],
        },
    )


def classify(
    head_sha: str,
    reviews: list[dict[str, object]],
    issue_comments: list[dict[str, object]],
    review_comments: list[dict[str, object]],
) -> dict[str, object]:
    """Return READY with a local review, or WAITING when Codex has not finished."""
    review_decision = _review_decision(head_sha, reviews, review_comments)
    comment_decision = _comment_decision(head_sha, issue_comments)

    candidates = [candidate for candidate in (review_decision, comment_decision) if candidate]
    if candidates:
        _, review = max(candidates, key=lambda candidate: candidate[0])
        return {"status": "READY", "review": review}

    has_matching_review = any(
        _is_codex(review) and _review_matches_head(review, head_sha) for review in reviews
    )
    if not has_matching_review and any(
        _is_codex(comment) and _summary_matches_head(comment, head_sha)
        for comment in issue_comments
    ):
        return {
            "status": "READY",
            "review": {
                "verdict": "APPROVE",
                "summary": (
                    f"Managed Codex completed review of {head_sha[:12]} with no published findings."
                ),
                "findings": [],
            },
        }

    return {"status": "WAITING"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify managed Codex GitHub review artifacts for one PR head."
    )
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--issue-comments", type=Path, required=True)
    parser.add_argument("--review-comments", type=Path, required=True)
    return parser


def main() -> int:
    """Run the managed-review classifier."""
    parser = _parser()
    args = parser.parse_args()
    try:
        result = classify(
            args.head_sha,
            _load_array(args.reviews),
            _load_array(args.issue_comments),
            _load_array(args.review_comments),
        )
    except (OSError, json.JSONDecodeError, ManagedReviewError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
