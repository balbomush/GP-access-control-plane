from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def ensure_clean(repo: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git status failed for {repo}")
    if result.stdout.strip():
        raise GitError(f"refusing to pull dirty repository: {repo}")


def pull_only(repos: list[Path]) -> None:
    for repo in repos:
        ensure_clean(repo)
    for repo in repos:
        result = subprocess.run(
            ["git", "-C", str(repo), "pull", "--ff-only"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or result.stdout.strip() or f"git pull failed for {repo}")
