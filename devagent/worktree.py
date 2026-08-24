from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreeSelection:
    root: Path
    isolated: bool
    reason: str


def select_worktree(source_root: Path, run_id: str, *, enabled: bool, git_head: str | None, dirty_files: list[str]) -> WorktreeSelection:
    """Create a retained detached worktree for a clean Git repository."""
    if not enabled:
        return WorktreeSelection(source_root, False, "isolation disabled by caller")
    if not git_head:
        return WorktreeSelection(source_root, False, "repository is not a Git worktree")
    if dirty_files:
        return WorktreeSelection(source_root, False, "repository has pre-existing changes; dirty files remain protected")
    destination = source_root / ".devagent" / "worktrees" / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["git", "worktree", "add", "--detach", str(destination), git_head],
            cwd=source_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WorktreeSelection(source_root, False, f"isolation unavailable: {exc}")
    if completed.returncode != 0:
        reason = completed.stderr.strip() or completed.stdout.strip() or "git worktree add failed"
        return WorktreeSelection(source_root, False, f"isolation unavailable: {reason}")
    return WorktreeSelection(destination.resolve(), True, "clean Git HEAD isolated in a retained detached worktree")
