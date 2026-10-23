from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUSH_SCRIPT = ROOT / "scripts" / "ai-safe-push.sh"
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required for AI publication tests")
GIT: str = _GIT


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


def _seed_remote(tmp_path: Path, *, branch: str = "feature") -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", branch, str(seed))
    _configure_identity(seed)
    (seed / "value.txt").write_text("base\n", encoding="utf-8")
    (seed / "CLAUDE.md").write_text("pr config\n", encoding="utf-8")
    _git(seed, "add", "value.txt", "CLAUDE.md")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", branch)
    return remote, _git(seed, "rev-parse", "HEAD").stdout.strip()


def _clone(remote: Path, destination: Path, *, branch: str = "feature") -> None:
    _git(
        destination.parent,
        "clone",
        "--branch",
        branch,
        str(remote),
        str(destination),
    )
    _configure_identity(destination)


def _run_safe_push(
    repo: Path,
    reviewed_head: str,
    *,
    head_ref: str = "feature",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HEAD_REF"] = head_ref
    env["EVENT_HEAD_SHA"] = reviewed_head
    return _run([str(PUSH_SCRIPT)], repo, env=env, check=False)


def _remote_head(tmp_path: Path, remote: Path, *, branch: str = "feature") -> str:
    return _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        f"refs/heads/{branch}",
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



def test_safe_push_rejects_main_without_changing_local_or_remote_head(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path, branch="main")
    claude = tmp_path / "claude"
    _clone(remote, claude, branch="main")
    (claude / "value.txt").write_text("fixed\n", encoding="utf-8")

    result = _run_safe_push(claude, reviewed_head, head_ref="main")

    assert result.returncode != 0
    assert "may not publish to main" in result.stderr
    assert _git(claude, "rev-parse", "HEAD").stdout.strip() == reviewed_head
    assert _remote_head(tmp_path, remote, branch="main") == reviewed_head


def test_safe_push_ignores_restored_root_symlink_replacement(tmp_path: Path) -> None:
    remote, _ = _seed_remote(tmp_path)
    author = tmp_path / "author"
    _clone(remote, author)
    (author / "claude-config").mkdir()
    (author / "claude-config" / "settings.json").write_text("{}\n", encoding="utf-8")
    os.symlink("claude-config", author / ".claude")
    _git(author, "add", ".claude", "claude-config/settings.json")
    _git(author, "commit", "-m", "test: track claude root symlink")
    reviewed_head = _git(author, "rev-parse", "HEAD").stdout.strip()
    _git(author, "push", "origin", "feature")

    claude = tmp_path / "claude"
    _clone(remote, claude)
    (claude / ".claude").unlink()
    (claude / ".claude").mkdir()
    (claude / ".claude" / "settings.json").write_text("restored\n", encoding="utf-8")
    (claude / "value.txt").write_text("fixed\n", encoding="utf-8")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode == 0, result.stderr
    published_head = _remote_head(tmp_path, remote)
    assert _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "show",
        f"{published_head}:.claude",
    ).stdout.strip() == "claude-config"


def test_safe_push_allows_env_example_but_rejects_real_env_file(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    _clone(remote, claude)
    (claude / ".env.example").write_text("API_KEY=placeholder\n", encoding="utf-8")

    allowed = _run_safe_push(claude, reviewed_head)

    assert allowed.returncode == 0, allowed.stderr
    published_head = _remote_head(tmp_path, remote)
    assert _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "show",
        f"{published_head}:.env.example",
    ).stdout == "API_KEY=placeholder\n"

    second = tmp_path / "second"
    _clone(remote, second)
    (second / ".env.local").write_text("API_KEY=secret\n", encoding="utf-8")

    blocked = _run_safe_push(second, published_head)

    assert blocked.returncode != 0
    assert "forbidden automated change '.env.local'" in blocked.stderr
    assert _git(second, "rev-parse", "HEAD").stdout.strip() == published_head
    assert _remote_head(tmp_path, remote) == published_head

def test_workflow_uses_wrapper_instead_of_direct_commit_permissions() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ai-pr-loop.yml").read_text(encoding="utf-8")
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

    assert "Bash(./scripts/ai-safe-push.sh)" in workflow
    assert "Bash(git commit" not in workflow
    assert "Bash(git push" not in workflow

    restricted = settings["permissions"]["ask"] + settings["permissions"]["deny"]
    assert all("ai-safe-push.sh" not in rule for rule in restricted)
