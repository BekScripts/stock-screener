"""Validate and render the fail-closed AI supervisor shadow prompt."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from collections.abc import Callable
from typing import cast

STATE_RELATIVE_PATH = Path(".ai/supervisor-state.json")
CONTRACT_RELATIVE_PATH = Path(".ai/SUPERVISOR_CONTRACT.md")
PROMPT_RELATIVE_PATH = Path(".ai/CODEX_SUPERVISOR_PROMPT.md")

_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required for AI supervisor state")
GIT: str = _GIT

EXPECTED_ACTION_KEYS = {
    "create_or_edit_issue",
    "create_or_switch_branch",
    "commit",
    "push",
    "open_or_edit_pr",
    "merge",
    "modify_workflow",
    "deploy",
    "mutate_runtime",
    "mutate_broker",
    "use_or_rotate_secrets",
    "authorize_live_trading",
}
EXPECTED_VERDICTS = {
    "READY_TASK",
    "HUMAN_REQUIRED",
    "ARCHITECTURE_BLOCKED",
    "NO_SAFE_TASK",
}


class SupervisorStateError(RuntimeError):
    """Raised when shadow-supervisor state is unsafe or incomplete."""


def _repo_root() -> Path:
    completed = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    )
    return Path(completed.stdout.strip()).resolve()


def _load_json_object(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SupervisorStateError(f"{path} must contain a JSON object")
    return cast(dict[str, object], raw)


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SupervisorStateError(f"{key!r} must be a non-empty string")
    return value


def _require_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SupervisorStateError(f"{key!r} must be an integer")
    return value


def _require_string_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise SupervisorStateError(f"{key!r} must be a list of non-empty strings")
    return cast(list[str], value)


def _safe_repo_path(root: Path, raw: str, *, label: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute():
        raise SupervisorStateError(f"{label} must be repository-relative")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SupervisorStateError(f"{label} escapes the repository") from exc
    return resolved


def _validate_state(root: Path) -> dict[str, object]:
    state_path = root / STATE_RELATIVE_PATH
    if not state_path.is_file():
        raise SupervisorStateError(f"missing supervisor state: {STATE_RELATIVE_PATH}")

    payload = _load_json_object(state_path)

    if _require_int(payload, "version") != 1:
        raise SupervisorStateError("unsupported supervisor-state version")
    if _require_str(payload, "mode") != "SHADOW":
        raise SupervisorStateError("supervisor must remain in SHADOW mode")
    if _require_int(payload, "max_tasks_proposed") != 1:
        raise SupervisorStateError("shadow supervisor may propose exactly one task")
    if _require_str(payload, "selection_policy") != "SMALLEST_SAFE_TRANSITION":
        raise SupervisorStateError("unexpected supervisor selection policy")

    actions_raw = payload.get("actions")
    if not isinstance(actions_raw, dict):
        raise SupervisorStateError("'actions' must be an object")
    actions = cast(dict[str, object], actions_raw)
    if set(actions) != EXPECTED_ACTION_KEYS:
        raise SupervisorStateError("supervisor action keys do not match the fail-closed schema")
    enabled = sorted(
        key
        for key, value in actions.items()
        if not isinstance(value, bool) or value
    )
    if enabled:
        raise SupervisorStateError(
            "shadow supervisor has mutation authority enabled: " + ", ".join(enabled)
        )

    verdicts = set(_require_string_list(payload, "allowed_verdicts"))
    if verdicts != EXPECTED_VERDICTS:
        raise SupervisorStateError("allowed supervisor verdicts changed unexpectedly")

    project_state = _require_str(payload, "authoritative_project_state")
    _safe_repo_path(root, project_state, label="authoritative_project_state")
    for index, source in enumerate(_require_string_list(payload, "planning_sources")):
        _safe_repo_path(root, source, label=f"planning_sources[{index}]")

    for required in (CONTRACT_RELATIVE_PATH, PROMPT_RELATIVE_PATH):
        if not (root / required).is_file():
            raise SupervisorStateError(f"missing supervisor control file: {required}")

    return payload


def _cmd_validate(_: argparse.Namespace) -> int:
    root = _repo_root()
    payload = _validate_state(root)
    project_state = _safe_repo_path(
        root,
        _require_str(payload, "authoritative_project_state"),
        label="authoritative_project_state",
    )
    print("Supervisor mode: SHADOW")
    print("Mutation authority: NONE")
    if project_state.is_file():
        print(f"Authoritative project state: {project_state.relative_to(root)}")
    else:
        print(
            "Authoritative project state: MISSING "
            f"({project_state.relative_to(root)})"
        )
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    root = _repo_root()
    payload = _validate_state(root)
    project_state_raw = _require_str(payload, "authoritative_project_state")
    project_state = _safe_repo_path(
        root,
        project_state_raw,
        label="authoritative_project_state",
    )
    if not project_state.is_file():
        raise SupervisorStateError(
            "cannot propose a task: authoritative project state is missing from "
            f"the current checkout ({project_state.relative_to(root)}); do not "
            "infer state from an unmerged branch"
        )

    planning_sources = _require_string_list(payload, "planning_sources")
    prompt = (root / PROMPT_RELATIVE_PATH).read_text(encoding="utf-8")
    state_text = json.dumps(payload, indent=2, sort_keys=True)
    planning_text = "\n".join(f"- {source}" for source in planning_sources)
    rendered = (
        prompt.replace("{{SUPERVISOR_STATE}}", state_text)
        .replace("{{PROJECT_STATE_PATH}}", project_state_raw)
        .replace("{{PLANNING_SOURCES}}", planning_text)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered.rstrip() + "\n", encoding="utf-8")
    print(args.output)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or render the read-only AI supervisor shadow prompt."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="verify that the supervisor remains fail-closed in shadow mode",
    )
    validate.set_defaults(func=_cmd_validate)

    render = subparsers.add_parser(
        "render",
        help="render the shadow supervisor prompt without mutating repository state",
    )
    render.add_argument("--output", type=Path, required=True)
    render.set_defaults(func=_cmd_render)

    return parser


def main() -> int:
    """Run the shadow-supervisor command-line interface."""
    parser = _parser()
    args = parser.parse_args()
    try:
        handler = cast(Callable[[argparse.Namespace], int], args.func)
        return handler(args)
    except (
        OSError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        SupervisorStateError,
    ) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
