from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUSH_SCRIPT = ROOT / "scripts" / "ai-safe-push.sh"
GIT = shutil.which("git")
if GIT is None:
    raise RuntimeError("git is required for AI publication tests")


def _run(
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run([GIT, *args], cwd, check=check)


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "AI Canary")
    _git(repo, "config", "user.email", "ai-canary@example.invalid")


def _seed_remote(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "feature", str(seed))
    _configure_identity(seed)
    (seed / "value.txt").write_text("base\n", encoding="utf-8")
    (seed / "CLAUDE.md").write_text("pr config\n", encoding="utf-8")
    _git(seed, "add", "value.txt", "CLAUDE.md")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "feature")
    return remote, _git(seed, "rev-parse", "HEAD").stdout.strip()


def _clone(remote: Path, destination: Path) -> None:
    _git(
        destination.parent,
        "clone",
        "--branch",
        "feature",
        str(remote),
        str(destination),
    )
    _configure_identity(destination)


def _run_safe_push(repo: Path, reviewed_head: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HEAD_REF"] = "feature"
    env["EVENT_HEAD_SHA"] = reviewed_head
    return _run([str(PUSH_SCRIPT)], repo, env=env, check=False)


def _remote_head(tmp_path: Path, remote: Path) -> str:
    return _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        "refs/heads/feature",
    ).stdout.strip()


def test_safe_push_commits_and_publishes_when_remote_is_unchanged(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    _clone(remote, claude)
    (claude / "value.txt").write_text("fixed\n", encoding="utf-8")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode == 0, result.stderr
    published_head = _remote_head(tmp_path, remote)
    parent = _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        f"{published_head}^",
    ).stdout.strip()
    assert parent == reviewed_head


def test_safe_push_rejects_concurrent_remote_update(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    concurrent = tmp_path / "concurrent"
    _clone(remote, claude)
    _clone(remote, concurrent)

    (claude / "value.txt").write_text("claude fix\n", encoding="utf-8")
    (concurrent / "value.txt").write_text("human update\n", encoding="utf-8")
    _git(concurrent, "add", "value.txt")
    _git(concurrent, "commit", "-m", "chore: concurrent update")
    concurrent_head = _git(concurrent, "rev-parse", "HEAD").stdout.strip()
    _git(concurrent, "push", "origin", "feature")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode != 0
    assert "remote branch advanced since review" in result.stderr
    assert _remote_head(tmp_path, remote) == concurrent_head
    assert _git(claude, "rev-parse", "HEAD").stdout.strip() == reviewed_head


def test_safe_push_preserves_action_restored_config_paths(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    _clone(remote, claude)
    (claude / "CLAUDE.md").write_text("base-restored config\n", encoding="utf-8")
    (claude / "value.txt").write_text("fixed\n", encoding="utf-8")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode == 0, result.stderr
    published_head = _remote_head(tmp_path, remote)
    remote_config = _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "show",
        f"{published_head}:CLAUDE.md",
    ).stdout
    assert remote_config == "pr config\n"


def test_workflow_uses_wrapper_instead_of_direct_commit_permissions() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ai-pr-loop.yml").read_text(encoding="utf-8")
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

    assert "Bash(./scripts/ai-safe-push.sh)" in workflow
    assert "Bash(git commit" not in workflow
    assert "Bash(git push" not in workflow

    restricted = settings["permissions"]["ask"] + settings["permissions"]["deny"]
    assert all("ai-safe-push.sh" not in rule for rule in restricted)
