"""Manage local task state for the Claude /task and /ship workflow."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

STATE_VERSION = 1
ISSUE_URL_RE = re.compile(r"/issues/(?P<number>[1-9][0-9]*)/?$")
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required for AI task state")
GIT: str = _GIT


class TaskStateError(RuntimeError):
    """Raised when the local AI task state is invalid."""


@dataclass(frozen=True)
class TaskState:
    """Local pointer to the durable GitHub task contract."""

    version: int
    issue_number: int
    issue_url: str
    title: str
    branch: str
    body: str
    created_at: str


def _run_git(args: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(  # noqa: S603
        [GIT, *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _repo_root() -> Path:
    return Path(_run_git(["rev-parse", "--show-toplevel"])).resolve()


def _git_dir(root: Path) -> Path:
    raw = Path(_run_git(["rev-parse", "--git-dir"], cwd=root))
    if raw.is_absolute():
        return raw.resolve()
    return (root / raw).resolve()


def _state_path(root: Path) -> Path:
    return _git_dir(root) / "ai-task.json"


def _current_branch(root: Path) -> str:
    branch = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root)
    if not branch:
        raise TaskStateError("AI tasks require a named branch, not detached HEAD")
    return branch


def _read_text(path: Path, *, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise TaskStateError(f"{label} must not be empty")
    return text.rstrip() + "\n"


def _read_state(root: Path) -> TaskState | None:
    path = _state_path(root)
    if not path.exists():
        return None
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TaskStateError("AI task state must be a JSON object")
    payload = cast("dict[str, object]", raw)

    def expect_str(key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise TaskStateError(f"AI task state field {key!r} must be a string")
        return value

    version = payload.get("version")
    issue_number = payload.get("issue_number")
    if not isinstance(version, int) or isinstance(version, bool):
        raise TaskStateError("AI task state field 'version' must be an integer")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        raise TaskStateError("AI task state field 'issue_number' must be an integer")

    state = TaskState(
        version=version,
        issue_number=issue_number,
        issue_url=expect_str("issue_url"),
        title=expect_str("title"),
        branch=expect_str("branch"),
        body=expect_str("body"),
        created_at=expect_str("created_at"),
    )
    if state.version != STATE_VERSION:
        raise TaskStateError(
            f"unsupported AI task state version {state.version}; expected {STATE_VERSION}"
        )
    return state


def _require_state(root: Path) -> TaskState:
    state = _read_state(root)
    if state is None:
        raise TaskStateError("no active task; start one with /task <prompt>")
    return state


def _write_state(root: Path, state: TaskState) -> None:
    path = _state_path(root)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_issue_number(issue_url: str) -> int:
    match = ISSUE_URL_RE.search(issue_url)
    if match is None:
        raise TaskStateError(f"issue URL must end with /issues/<number>, got {issue_url!r}")
    return int(match.group("number"))


def _bind(args: argparse.Namespace) -> int:
    root = _repo_root()
    existing = _read_state(root)
    if existing is not None:
        raise TaskStateError(
            f"task #{existing.issue_number} is already active on "
            f"{existing.branch}; clear it before binding another task"
        )

    branch = args.branch
    current = _current_branch(root)
    if branch != current:
        raise TaskStateError(
            f"refusing task bind: requested branch {branch!r} "
            f"does not match current branch {current!r}"
        )
    if branch in {"main", "master"}:
        raise TaskStateError("refusing to bind an AI task directly to the default branch")

    title = _read_text(args.title_file, label="task title").strip()
    body = _read_text(args.body_file, label="task body")
    issue_number = _parse_issue_number(args.issue_url)
    state = TaskState(
        version=STATE_VERSION,
        issue_number=issue_number,
        issue_url=args.issue_url,
        title=title,
        branch=branch,
        body=body,
        created_at=datetime.now(UTC).isoformat(),
    )
    _write_state(root, state)
    print(f"bound task #{issue_number} to {branch}")
    return 0


def _status(args: argparse.Namespace) -> int:
    root = _repo_root()
    state = _read_state(root)
    if state is None:
        if args.json:
            print("null")
        else:
            print("No active task.")
        return 0

    if args.json:
        print(json.dumps(asdict(state), indent=2, sort_keys=True))
        return 0

    print(f"Task #{state.issue_number}: {state.title}")
    print(f"Issue:  {state.issue_url}")
    print(f"Branch: {state.branch}")
    print(f"State:  {_state_path(root)}")
    return 0


def _context(_: argparse.Namespace) -> int:
    root = _repo_root()
    state = _read_state(root)
    if state is None:
        return 0

    print("ACTIVE DEVELOPMENT TASK")
    print(f"Issue #{state.issue_number}: {state.title}")
    print(f"Branch: {state.branch}")
    print(f"Issue: {state.issue_url}")
    print()
    print("Task contract:")
    print(state.body.rstrip())
    print()
    print("Task-boundary rules:")
    print("- Normal conversation stays within this task.")
    print("- Only /task starts or explicitly updates task requirements.")
    print("- /ship prepares one pull request for this task.")
    print("- Do not infer a new task from ordinary chat.")
    return 0


def _prepare_update(args: argparse.Namespace) -> int:
    root = _repo_root()
    state = _require_state(root)
    update_text = _read_text(args.text_file, label="task update").rstrip()
    body = state.body.rstrip()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if "## Requirement updates" not in body:
        body += (
            "\n\n## Requirement updates\n\n"
            "Later entries in this section override earlier conflicting "
            "requirements. The original prompt above remains unchanged.\n"
        )

    body += f"\n### {timestamp}\n\n{update_text}\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8")
    print(args.output)
    return 0


def _sync_body(args: argparse.Namespace) -> int:
    root = _repo_root()
    state = _require_state(root)
    body = _read_text(args.body_file, label="task body")
    _write_state(
        root,
        TaskState(
            version=state.version,
            issue_number=state.issue_number,
            issue_url=state.issue_url,
            title=state.title,
            branch=state.branch,
            body=body,
            created_at=state.created_at,
        ),
    )
    print(f"updated local task context for #{state.issue_number}")
    return 0


def _render_pr(args: argparse.Namespace) -> int:
    root = _repo_root()
    state = _require_state(root)
    current = _current_branch(root)
    if current != state.branch:
        raise TaskStateError(
            f"refusing PR body render: task belongs to {state.branch!r}, "
            f"current branch is {current!r}"
        )

    implementation = _read_text(
        args.implementation_file,
        label="implementation summary",
    ).rstrip()
    testing = _read_text(args.testing_file, label="testing summary").rstrip()

    body = (
        f"{state.body.rstrip()}\n\n"
        f"## Implementation\n\n{implementation}\n\n"
        f"## Testing\n\n{testing}\n\n"
        f"## Task\n\nCloses #{state.issue_number}\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8")
    print(args.output)
    return 0


def _clear(_: argparse.Namespace) -> int:
    root = _repo_root()
    path = _state_path(root)
    if path.exists():
        path.unlink()
        print("cleared local AI task state")
    else:
        print("No active task.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage local task state for the Claude /task and /ship workflow."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bind = subparsers.add_parser("bind", help="bind a GitHub issue to this branch")
    bind.add_argument("--issue-url", required=True)
    bind.add_argument("--branch", required=True)
    bind.add_argument("--title-file", type=Path, required=True)
    bind.add_argument("--body-file", type=Path, required=True)
    bind.set_defaults(func=_bind)

    status = subparsers.add_parser("status", help="show the active task")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status)

    context = subparsers.add_parser(
        "context",
        help="print task context for the Claude SessionStart hook",
    )
    context.set_defaults(func=_context)

    prepare_update = subparsers.add_parser(
        "prepare-update",
        help="append an explicit requirement update to the task body",
    )
    prepare_update.add_argument("--text-file", type=Path, required=True)
    prepare_update.add_argument("--output", type=Path, required=True)
    prepare_update.set_defaults(func=_prepare_update)

    sync_body = subparsers.add_parser(
        "sync-body",
        help="replace the locally cached task body after a GitHub issue update",
    )
    sync_body.add_argument("--body-file", type=Path, required=True)
    sync_body.set_defaults(func=_sync_body)

    render_pr = subparsers.add_parser(
        "render-pr",
        help="render the PR body from task context plus implementation evidence",
    )
    render_pr.add_argument("--implementation-file", type=Path, required=True)
    render_pr.add_argument("--testing-file", type=Path, required=True)
    render_pr.add_argument("--output", type=Path, required=True)
    render_pr.set_defaults(func=_render_pr)

    clear = subparsers.add_parser("clear", help="clear local task state")
    clear.set_defaults(func=_clear)

    return parser


def main() -> int:
    """Run the task-state command-line interface."""
    parser = _parser()
    args = parser.parse_args()
    try:
        handler = cast("Callable[[argparse.Namespace], int]", args.func)
        return handler(args)
    except (OSError, subprocess.CalledProcessError, TaskStateError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
