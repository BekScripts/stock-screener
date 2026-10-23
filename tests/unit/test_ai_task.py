from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK_SCRIPT = ROOT / "scripts" / "ai_task.py"
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required for AI task tests")
GIT: str = _GIT


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


def _init_repo(tmp_path: Path, *, branch: str = "feat/example") -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-b", branch, str(repo))
    _git(repo, "config", "user.name", "AI Task Test")
    _git(repo, "config", "user.email", "ai-task@example.invalid")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "test: seed repo")
    return repo


def _task(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(TASK_SCRIPT), *args], repo, check=check)


def _bind(repo: Path, tmp_path: Path, *, issue_number: int = 42) -> str:
    original_prompt = (
        "Build the execution engine exactly as specified.\nDo not rerank persisted decisions.\n"
    )
    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.md"
    title_file.write_text("Phase 2 execution engine\n", encoding="utf-8")
    body_file.write_text(
        "## Original task / prompt\n\n"
        f"{original_prompt}\n"
        "## Acceptance criteria\n\n"
        "- Consume persisted decisions.\n\n"
        "## Constraints\n\n"
        "- PAPER only.\n",
        encoding="utf-8",
    )
    result = _task(
        repo,
        "bind",
        "--issue-url",
        f"https://github.com/example/project/issues/{issue_number}",
        "--branch",
        "feat/example",
        "--title-file",
        str(title_file),
        "--body-file",
        str(body_file),
    )
    assert result.returncode == 0
    return original_prompt


def test_bind_stores_task_only_in_git_metadata_and_context_is_verbatim(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    original_prompt = _bind(repo, tmp_path)

    state_path = repo / ".git" / "ai-task.json"
    assert state_path.exists()
    assert not (repo / "ai-task.json").exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["issue_number"] == 42
    assert state["branch"] == "feat/example"
    assert original_prompt in state["body"]

    context = _task(repo, "context").stdout
    assert "ACTIVE DEVELOPMENT TASK" in context
    assert "Issue #42: Phase 2 execution engine" in context
    assert original_prompt in context
    assert "Do not infer a new task from ordinary chat." in context


def test_bind_rejects_default_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, branch="main")
    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.md"
    title_file.write_text("Unsafe task\n", encoding="utf-8")
    body_file.write_text("task body\n", encoding="utf-8")

    result = _task(
        repo,
        "bind",
        "--issue-url",
        "https://github.com/example/project/issues/7",
        "--branch",
        "main",
        "--title-file",
        str(title_file),
        "--body-file",
        str(body_file),
        check=False,
    )

    assert result.returncode != 0
    assert "default branch" in result.stderr
    assert not (repo / ".git" / "ai-task.json").exists()


def test_requirement_update_preserves_original_prompt_and_renders_pr(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    original_prompt = _bind(repo, tmp_path)
    update_text = "Execution must also reject stale persisted decisions.\n"
    update_file = tmp_path / "update.md"
    updated_body = tmp_path / "updated-body.md"
    update_file.write_text(update_text, encoding="utf-8")

    prepared = _task(
        repo,
        "prepare-update",
        "--text-file",
        str(update_file),
        "--output",
        str(updated_body),
    )

    assert prepared.returncode == 0
    body = updated_body.read_text(encoding="utf-8")
    assert original_prompt in body
    assert "## Requirement updates" in body
    assert update_text.strip() in body

    _task(repo, "sync-body", "--body-file", str(updated_body))

    implementation = tmp_path / "implementation.md"
    testing = tmp_path / "testing.md"
    pr_body = tmp_path / "pr-body.md"
    implementation.write_text(
        "- Added persisted-decision execution.\n",
        encoding="utf-8",
    )
    testing.write_text("- make check — passed.\n", encoding="utf-8")

    rendered = _task(
        repo,
        "render-pr",
        "--implementation-file",
        str(implementation),
        "--testing-file",
        str(testing),
        "--output",
        str(pr_body),
    )

    assert rendered.returncode == 0
    result = pr_body.read_text(encoding="utf-8")
    assert original_prompt in result
    assert update_text.strip() in result
    assert "## Implementation" in result
    assert "## Testing" in result
    assert "Closes #42" in result


def test_render_pr_rejects_wrong_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _bind(repo, tmp_path)
    _git(repo, "switch", "-c", "feat/other")
    implementation = tmp_path / "implementation.md"
    testing = tmp_path / "testing.md"
    implementation.write_text("implemented\n", encoding="utf-8")
    testing.write_text("tested\n", encoding="utf-8")

    result = _task(
        repo,
        "render-pr",
        "--implementation-file",
        str(implementation),
        "--testing-file",
        str(testing),
        "--output",
        str(tmp_path / "pr.md"),
        check=False,
    )

    assert result.returncode != 0
    assert "task belongs to 'feat/example'" in result.stderr


def test_project_wires_user_only_task_skills_and_session_context() -> None:
    task_skill = (ROOT / ".agents" / "skills" / "task" / "SKILL.md").read_text(encoding="utf-8")
    ship_skill = (ROOT / ".agents" / "skills" / "ship" / "SKILL.md").read_text(encoding="utf-8")
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

    assert "disable-model-invocation: true" in task_skill
    assert "$ARGUMENTS" in task_skill
    assert "disable-model-invocation: true" in ship_skill
    assert "gh pr create" in ship_skill

    hooks = settings["hooks"]["SessionStart"]
    command = hooks[0]["hooks"][0]["command"]
    assert "scripts/ai_task.py context" in command
    assert "Bash(gh issue create:*)" in settings["permissions"]["ask"]
    assert "Bash(gh issue edit:*)" in settings["permissions"]["ask"]
