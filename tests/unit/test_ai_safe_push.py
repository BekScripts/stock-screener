from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PUSH_SCRIPT = ROOT / "scripts" / "ai-safe-push.sh"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


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
    _git(seed, "add", "value.txt")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "feature")

    return remote, _git(seed, "rev-parse", "HEAD").stdout.strip()


def _clone(remote: Path, destination: Path) -> None:
    _git(destination.parent, "clone", "--branch", "feature", str(remote), str(destination))
    _configure_identity(destination)


def _commit(repo: Path, text: str, message: str) -> str:
    (repo / "value.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", "value.txt")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_safe_push(repo: Path, reviewed_head: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HEAD_REF"] = "feature"
    env["EVENT_HEAD_SHA"] = reviewed_head
    return subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_safe_push_accepts_direct_child_when_remote_is_unchanged(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    _clone(remote, claude)

    fix_head = _commit(claude, "fixed\n", "fix: review finding")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode == 0, result.stderr
    remote_head = _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        "refs/heads/feature",
    ).stdout.strip()
    assert remote_head == fix_head


def test_safe_push_rejects_concurrent_remote_update(tmp_path: Path) -> None:
    remote, reviewed_head = _seed_remote(tmp_path)
    claude = tmp_path / "claude"
    concurrent = tmp_path / "concurrent"
    _clone(remote, claude)
    _clone(remote, concurrent)

    _commit(claude, "claude fix\n", "fix: review finding")
    concurrent_head = _commit(concurrent, "human update\n", "chore: concurrent update")
    _git(concurrent, "push", "origin", "feature")

    result = _run_safe_push(claude, reviewed_head)

    assert result.returncode != 0
    assert "remote branch advanced since review" in result.stderr
    remote_head = _git(
        tmp_path,
        "--git-dir",
        str(remote),
        "rev-parse",
        "refs/heads/feature",
    ).stdout.strip()
    assert remote_head == concurrent_head
