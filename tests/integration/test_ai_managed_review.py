"""Integration tests for the managed Codex GitHub review adapter."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ai_managed_review.py"
HEAD = "abcdef1234567890abcdef1234567890abcdef12"
BOT = "chatgpt-codex-connector[bot]"

pytestmark = pytest.mark.integration


def _write(path: Path, payload: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _classify(
    tmp_path: Path,
    *,
    reviews: list[dict[str, object]] | None = None,
    issue_comments: list[dict[str, object]] | None = None,
    review_comments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    reviews_path = tmp_path / "reviews.json"
    issues_path = tmp_path / "issues.json"
    review_comments_path = tmp_path / "review-comments.json"
    _write(reviews_path, reviews or [])
    _write(issues_path, issue_comments or [])
    _write(review_comments_path, review_comments or [])

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--head-sha",
            HEAD,
            "--reviews",
            str(reviews_path),
            "--issue-comments",
            str(issues_path),
            "--review-comments",
            str(review_comments_path),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    raw: object = json.loads(completed.stdout)
    assert isinstance(raw, dict)
    return raw


def _bot() -> dict[str, object]:
    return {"login": BOT}


def _review(*, submitted_at: str = "2026-10-23T19:00:00Z") -> dict[str, object]:
    return {
        "id": 42,
        "state": "COMMENTED",
        "commit_id": HEAD,
        "submitted_at": submitted_at,
        "body": "### 💡 Codex Review\n\nHere are some automated review suggestions.",
        "user": _bot(),
    }


def _finding(
    body: str,
    *,
    path: str = "src/example.py",
    line: int = 17,
) -> dict[str, object]:
    return {
        "id": 99,
        "pull_request_review_id": 42,
        "body": body,
        "path": path,
        "line": line,
        "user": _bot(),
    }


def _review_payload(result: dict[str, object]) -> dict[str, object]:
    review = result.get("review")
    assert isinstance(review, dict)
    return review


def test_clean_codex_comment_approves_exact_reviewed_head(tmp_path: Path) -> None:
    comment = {
        "id": 1,
        "created_at": "2026-10-23T19:01:00Z",
        "user": _bot(),
        "body": (
            f"Codex Review: Didn't find any major issues. 👍\n\n**Reviewed commit:** `{HEAD[:10]}`"
        ),
    }

    result = _classify(tmp_path, issue_comments=[comment])

    assert result["status"] == "READY"
    assert _review_payload(result)["verdict"] == "APPROVE"


def test_stale_clean_comment_does_not_approve_current_head(tmp_path: Path) -> None:
    comment = {
        "id": 1,
        "created_at": "2026-10-23T19:01:00Z",
        "user": _bot(),
        "body": "Codex Review: Didn't find any major issues. 👍\n\n**Reviewed commit:** `1234567890`",
    }

    result = _classify(tmp_path, issue_comments=[comment])

    assert result == {"status": "WAITING"}


def test_inline_codex_finding_becomes_changes_requested(tmp_path: Path) -> None:
    finding = _finding(
        "**<sub><sub>![P1 Badge](https://example.invalid/p1)</sub></sub> "
        "Reject stale authority**\n\nThe persisted authority is stale."
    )

    result = _classify(tmp_path, reviews=[_review()], review_comments=[finding])
    review = _review_payload(result)
    findings = review["findings"]

    assert review["verdict"] == "CHANGES_REQUESTED"
    assert isinstance(findings, list)
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["file"] == "src/example.py"
    assert findings[0]["line"] == 17


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("HUMAN_REQUIRED", "HUMAN_REQUIRED"),
        ("ARCHITECTURE_BLOCKED", "ARCHITECTURE_BLOCKED"),
    ],
)
def test_stop_tokens_prevent_automatic_fix(
    tmp_path: Path,
    token: str,
    expected: str,
) -> None:
    finding = _finding(
        "**["
        + token
        + "] owner decision required**"
        + "\n\nDo not choose this authority automatically."
    )

    result = _classify(tmp_path, reviews=[_review()], review_comments=[finding])

    assert _review_payload(result)["verdict"] == expected


def test_review_body_stop_marker_overrides_ordinary_inline_finding(
    tmp_path: Path,
) -> None:
    review = _review()
    review["body"] = (
        "### 💡 Codex Review\n\n"
        "HUMAN_REQUIRED: owner authorization is required before this transition."
    )
    finding = _finding("**P2 ordinary defect**\n\nThis can be fixed in code.")

    result = _classify(tmp_path, reviews=[review], review_comments=[finding])
    payload = _review_payload(result)
    findings = payload["findings"]

    assert payload["verdict"] == "HUMAN_REQUIRED"
    assert isinstance(findings, list)
    assert "HUMAN_REQUIRED" in findings[0]["details"]


def test_newer_clean_comment_overrides_older_finding_on_same_head(tmp_path: Path) -> None:
    finding = _finding("**P1 old finding**\n\nAn earlier pass found this.")
    clean = {
        "id": 2,
        "created_at": "2026-10-23T19:05:00Z",
        "user": _bot(),
        "body": (
            f"Codex Review: Didn't find any major issues. 👍\n\n**Reviewed commit:** `{HEAD[:12]}`"
        ),
    }

    result = _classify(
        tmp_path,
        reviews=[_review(submitted_at="2026-10-23T19:00:00Z")],
        issue_comments=[clean],
        review_comments=[finding],
    )

    assert _review_payload(result)["verdict"] == "APPROVE"


def test_completed_summary_is_clean_fallback_when_no_review_was_submitted(
    tmp_path: Path,
) -> None:
    summary = {
        "id": 3,
        "updated_at": "2026-10-23T19:06:00Z",
        "user": _bot(),
        "body": (
            "<!-- codex-pull-request-review-summary -->\n\n"
            "## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            f"| 📝 **Code Review** | ✅ **Completed** | `{HEAD[:7]}` | PR opened |"
        ),
    }

    result = _classify(tmp_path, issue_comments=[summary])

    assert _review_payload(result)["verdict"] == "APPROVE"


def test_summary_does_not_mix_completed_old_head_with_pending_current_head(
    tmp_path: Path,
) -> None:
    summary = {
        "id": 4,
        "updated_at": "2026-10-23T19:07:00Z",
        "user": _bot(),
        "body": (
            "<!-- codex-pull-request-review-summary -->\n\n"
            "## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            "| 📝 **Code Review** | ✅ **Completed** | `1234567` | earlier push |\n"
            f"| 📝 **Code Review** | ⏳ **In progress** | `{HEAD[:7]}` | latest push |"
        ),
    }

    result = _classify(tmp_path, issue_comments=[summary])

    assert result == {"status": "WAITING"}


def test_non_codex_feedback_is_ignored(tmp_path: Path) -> None:
    review = _review()
    review["user"] = {"login": "someone-else"}

    result = _classify(
        tmp_path,
        reviews=[review],
        review_comments=[_finding("**P1 finding**\n\nBad thing.")],
    )

    assert result == {"status": "WAITING"}
