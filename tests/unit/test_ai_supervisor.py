from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_SCRIPT = ROOT / "scripts" / "ai_supervisor.py"
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required for AI supervisor tests")
GIT: str = _GIT

ACTION_KEYS = {
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


def _run(
    argv: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([GIT, *args], cwd)


def _base_state() -> dict[str, object]:
    return {
        "version": 1,
        "mode": "SHADOW",
        "current_checkpoint": None,
        "current_task": None,
        "authoritative_project_state": "docs/reference/PROJECT_STATE.md",
        "planning_sources": ["docs/reference/roadmap.md"],
        "max_tasks_proposed": 1,
        "selection_policy": "SMALLEST_SAFE_TRANSITION",
        "actions": dict.fromkeys(ACTION_KEYS, False),
        "allowed_verdicts": [
            "READY_TASK",
            "HUMAN_REQUIRED",
            "ARCHITECTURE_BLOCKED",
            "NO_SAFE_TASK",
        ],
        "notes": ["test"],
    }


def _init_repo(tmp_path: Path, state: dict[str, object] | None = None) -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-b", "main", str(repo))
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "SUPERVISOR_CONTRACT.md").write_text(
        (ROOT / ".ai" / "SUPERVISOR_CONTRACT.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / ".ai" / "CODEX_SUPERVISOR_PROMPT.md").write_text(
        (ROOT / ".ai" / "CODEX_SUPERVISOR_PROMPT.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / ".ai" / "supervisor-state.json").write_text(
        json.dumps(state or _base_state(), indent=2) + "\n",
        encoding="utf-8",
    )
    return repo


def _commit_all(repo: Path, message: str = "commit") -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )


def _repo_with_state_on_main(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path)
    reference = repo / "docs" / "reference"
    reference.mkdir(parents=True)
    state = reference / "PROJECT_STATE.md"
    state.write_text("# Project state\n", encoding="utf-8")
    (reference / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    _commit_all(repo, "main")
    return repo, state


def _supervisor(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(SUPERVISOR_SCRIPT), *args], repo, check=check)


def test_checked_in_supervisor_state_is_shadow_and_has_no_mutation_authority() -> None:
    raw: object = json.loads((ROOT / ".ai" / "supervisor-state.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    state = raw
    actions = state["actions"]
    assert isinstance(actions, dict)

    assert state["mode"] == "SHADOW"
    assert state["max_tasks_proposed"] == 1
    assert set(actions) == ACTION_KEYS
    assert not any(actions.values())


def test_validate_rejects_any_enabled_mutation_action(tmp_path: Path) -> None:
    state = _base_state()
    actions = state["actions"]
    assert isinstance(actions, dict)
    actions["push"] = True
    repo = _init_repo(tmp_path, state)

    result = _supervisor(repo, "validate", check=False)

    assert result.returncode != 0
    assert "mutation authority enabled: push" in result.stderr


def test_validate_rejects_repository_path_escape(tmp_path: Path) -> None:
    state = _base_state()
    state["authoritative_project_state"] = "../outside.md"
    repo = _init_repo(tmp_path, state)

    result = _supervisor(repo, "validate", check=False)

    assert result.returncode != 0
    assert "escapes the repository" in result.stderr


def test_render_refuses_to_infer_state_from_an_unmerged_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    result = _supervisor(
        repo,
        "render",
        "--output",
        str(tmp_path / "prompt.md"),
        check=False,
    )

    assert result.returncode != 0
    assert "authoritative project state is missing" in result.stderr
    assert "do not infer state from an unmerged branch" in result.stderr


def test_render_keeps_roadmap_selective_instead_of_preloading_it(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    reference = repo / "docs" / "reference"
    reference.mkdir(parents=True)
    (reference / "PROJECT_STATE.md").write_text(
        "# Project state\n\n## Current phase\nP2.TEST\n",
        encoding="utf-8",
    )
    roadmap_sentinel = "ROADMAP-CONTENT-MUST-NOT-BE-PRELOADED"
    (reference / "roadmap.md").write_text(
        f"# Roadmap\n\n{roadmap_sentinel}\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    output = repo / ".claude" / "tmp" / "prompt.md"

    result = _supervisor(repo, "render", "--output", str(output))

    assert result.returncode == 0
    rendered = output.read_text(encoding="utf-8")
    assert "docs/reference/PROJECT_STATE.md" in rendered
    assert "docs/reference/roadmap.md" in rendered
    assert roadmap_sentinel not in rendered
    assert '"mode": "SHADOW"' in rendered
    assert '"push": false' in rendered


def test_render_rejects_untracked_project_state(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_all(repo, "main")
    reference = repo / "docs" / "reference"
    reference.mkdir(parents=True)
    (reference / "PROJECT_STATE.md").write_text("# State\n", encoding="utf-8")

    result = _supervisor(repo, "render", check=False)

    assert result.returncode != 0
    assert "untracked" in result.stderr


def test_render_rejects_state_added_only_on_a_feature_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_all(repo, "main")
    _git(repo, "checkout", "-b", "feature")
    reference = repo / "docs" / "reference"
    reference.mkdir(parents=True)
    (reference / "PROJECT_STATE.md").write_text("# State\n", encoding="utf-8")
    _commit_all(repo, "feature")

    result = _supervisor(repo, "render", check=False)

    assert result.returncode != 0
    assert "not on main" in result.stderr


def test_render_rejects_state_modified_only_on_a_feature_branch(
    tmp_path: Path,
) -> None:
    repo, state = _repo_with_state_on_main(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    state.write_text("# Forged state\n", encoding="utf-8")
    _commit_all(repo, "feature")

    result = _supervisor(repo, "render", check=False)

    assert result.returncode != 0
    assert "differs from main" in result.stderr


def test_render_rejects_dirty_project_state(tmp_path: Path) -> None:
    repo, state = _repo_with_state_on_main(tmp_path)
    state.write_text("# Edited\n", encoding="utf-8")

    result = _supervisor(repo, "render", check=False)

    assert result.returncode != 0
    assert "dirty" in result.stderr


def test_render_accepts_state_matching_main_and_prints_to_stdout(
    tmp_path: Path,
) -> None:
    repo, _ = _repo_with_state_on_main(tmp_path)
    _git(repo, "checkout", "-b", "feature")

    result = _supervisor(repo, "render")

    assert '"mode": "SHADOW"' in result.stdout


def test_render_refuses_outputs_outside_the_scratch_directory(tmp_path: Path) -> None:
    repo, _ = _repo_with_state_on_main(tmp_path)
    protected = repo / "AGENTS.md"
    protected.write_text("original\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("original\n", encoding="utf-8")

    for target in (
        protected,
        outside,
        repo / ".claude" / "tmp" / ".." / ".." / "AGENTS.md",
        repo / ".claude" / "tmp" / "x.txt",
    ):
        result = _supervisor(repo, "render", "--output", str(target), check=False)
        assert result.returncode != 0

    assert protected.read_text(encoding="utf-8") == "original\n"
    assert outside.read_text(encoding="utf-8") == "original\n"


def test_render_rejects_authority_declaration_redirected_on_a_feature_branch(
    tmp_path: Path,
) -> None:
    repo, _ = _repo_with_state_on_main(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    state = _base_state()
    state["authoritative_project_state"] = "docs/reference/roadmap.md"
    (repo / ".ai" / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")
    _commit_all(repo, "redirect")

    result = _supervisor(repo, "render", check=False)

    assert result.returncode != 0
    assert "differs from the declaration" in result.stderr


def test_render_does_not_write_through_a_hard_linked_output(tmp_path: Path) -> None:
    repo, _ = _repo_with_state_on_main(tmp_path)
    protected = repo / "AGENTS.md"
    protected.write_text("original\n", encoding="utf-8")
    scratch = repo / ".claude" / "tmp"
    scratch.mkdir(parents=True)
    target = scratch / "out.md"
    target.hardlink_to(protected)

    _supervisor(repo, "render", "--output", str(target))

    assert protected.read_text(encoding="utf-8") == "original\n"
    assert '"mode": "SHADOW"' in target.read_text(encoding="utf-8")


def test_render_refuses_symlinked_output(tmp_path: Path) -> None:
    repo, _ = _repo_with_state_on_main(tmp_path)
    victim = tmp_path / "victim.md"
    victim.write_text("original\n", encoding="utf-8")
    scratch = repo / ".claude" / "tmp"
    scratch.mkdir(parents=True)
    (scratch / "link.md").symlink_to(victim)
    (repo / "linkdir").symlink_to(tmp_path)
    (scratch / "dir").symlink_to(tmp_path)

    for target in (scratch / "link.md", scratch / "dir" / "victim.md"):
        result = _supervisor(repo, "render", "--output", str(target), check=False)
        assert result.returncode != 0

    assert victim.read_text(encoding="utf-8") == "original\n"


def test_supervise_skill_is_advisory_and_does_not_call_mutation_commands() -> None:
    skill = (ROOT / ".agents" / "skills" / "supervise" / "SKILL.md").read_text(encoding="utf-8")

    assert "disable-model-invocation: true" in skill
    assert "advisory only" in skill
    assert "gh issue create" not in skill
    assert "gh issue edit" not in skill
    assert "gh pr create" not in skill
    assert "gh pr merge" not in skill
    assert "git commit" not in skill
    assert "git push" not in skill
    assert "must never invoke `/task` or `/ship` automatically" in skill
