from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreeSelection:
    root: Path
    isolated: bool
    reason: str
    creation_failed: bool = False


def _repository_identifier(source_root: Path) -> str:
    identity = str(source_root.resolve()).encode("utf-8", errors="surrogatepass")
    return "repo-" + hashlib.sha256(identity).hexdigest()[:20]


def _state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "state").resolve()


def _active_worktrees(source_root: Path) -> tuple[list[Path], str | None]:
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=source_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if completed.returncode != 0:
        reason = completed.stderr.strip() or completed.stdout.strip() or "git worktree list failed"
        return [], reason
    return (
        [
            Path(line.removeprefix("worktree ")).resolve()
            for line in completed.stdout.splitlines()
            if line.startswith("worktree ")
        ],
        None,
    )


def select_worktree(
    source_root: Path,
    run_id: str,
    *,
    enabled: bool,
    git_head: str | None,
    dirty_files: list[str],
    state_root: Path | None = None,
) -> WorktreeSelection:
    """Create a retained detached worktree outside every active Git worktree."""
    source_root = source_root.expanduser().resolve()
    if not enabled:
        return WorktreeSelection(source_root, False, "isolation disabled by caller")
    if not git_head:
        return WorktreeSelection(source_root, False, "repository is not a Git worktree")
    if dirty_files:
        return WorktreeSelection(
            source_root,
            False,
            "repository has pre-existing changes; dirty files remain protected",
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        return WorktreeSelection(
            source_root,
            False,
            "isolation unavailable: run identifier is not a safe path segment",
            True,
        )

    base = (state_root or _state_root()).expanduser().resolve()
    destination = (
        base / "devagent" / "worktrees" / _repository_identifier(source_root) / run_id
    ).resolve()
    active_worktrees, enumeration_error = _active_worktrees(source_root)
    if enumeration_error is not None:
        return WorktreeSelection(
            source_root,
            False,
            f"isolation unavailable: cannot enumerate active worktrees: {enumeration_error}",
            True,
        )
    if any(
        destination == active or destination.is_relative_to(active)
        for active in active_worktrees
    ):
        return WorktreeSelection(
            source_root,
            False,
            "isolation unavailable: retained worktree destination is inside an active worktree",
            True,
        )
    if destination.exists():
        return WorktreeSelection(
            source_root,
            False,
            "isolation unavailable: retained worktree destination already exists",
            True,
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return WorktreeSelection(
            source_root,
            False,
            f"isolation unavailable: cannot create retained worktree parent: {exc}",
            True,
        )
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
        return WorktreeSelection(
            source_root,
            False,
            f"isolation unavailable: git worktree add failed safely: {exc}",
            True,
        )
    if completed.returncode != 0:
        reason = completed.stderr.strip() or completed.stdout.strip() or "git worktree add failed"
        return WorktreeSelection(
            source_root,
            False,
            f"isolation unavailable: {reason}",
            True,
        )
    return WorktreeSelection(
        destination,
        True,
        "clean Git HEAD isolated in a retained external detached worktree",
    )
