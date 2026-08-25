from __future__ import annotations

import re
import subprocess
from pathlib import Path

from devagent.models import Outcome, RunResult, SourceControlResult
from devagent.safety import is_secret_path


_PROTECTED_BRANCHES = {"main", "master", "trunk"}
_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]+$")


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _failure(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "unknown git error").strip()
    return f"{prefix}: {detail[:2000]}"


def _validate_path(path: str) -> bool:
    candidate = Path(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts and not is_secret_path(candidate)


def publish_verified_branch(
    result: RunResult,
    *,
    branch: str | None = None,
    remote: str = "origin",
) -> SourceControlResult:
    """Commit and push a VERIFIED result to one new branch, without PR or merge actions."""

    target_branch = branch or f"devagent/{result.run_id}"
    publication = SourceControlResult(requested=True, remote=remote, branch=target_branch)

    if result.outcome is not Outcome.VERIFIED:
        publication.error = "Branch publishing is allowed only for VERIFIED runs"
        return publication

    source_root = Path(result.repository.root).expanduser().resolve()
    working_root = Path(result.working_root).expanduser().resolve()
    if working_root == source_root:
        publication.error = "Branch publishing requires the default isolated worktree; remove --no-isolation"
        return publication
    if not working_root.is_dir():
        publication.error = f"Working root does not exist: {working_root}"
        return publication

    if not _SAFE_REMOTE.fullmatch(remote):
        publication.error = "Remote name contains unsupported characters"
        return publication
    if target_branch.lower() in _PROTECTED_BRANCHES:
        publication.error = f"Refusing to publish directly to protected branch: {target_branch}"
        return publication
    if not result.changes.paths:
        publication.error = "No reviewed file changes are available to commit"
        return publication
    invalid_paths = [path for path in result.changes.paths if not _validate_path(path)]
    if invalid_paths:
        publication.error = f"Refusing to stage unsafe path: {invalid_paths[0]}"
        return publication

    branch_check = _git(working_root, "check-ref-format", "--branch", target_branch, timeout=10)
    if branch_check.returncode != 0:
        publication.error = _failure("Invalid branch name", branch_check)
        return publication

    remote_check = _git(working_root, "remote", "get-url", remote, timeout=10)
    if remote_check.returncode != 0:
        publication.error = _failure(f"Git remote {remote!r} is unavailable", remote_check)
        return publication

    local_check = _git(working_root, "show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}", timeout=10)
    if local_check.returncode == 0:
        publication.error = f"Local branch already exists: {target_branch}"
        return publication

    remote_branch_check = _git(
        working_root,
        "ls-remote",
        "--exit-code",
        "--heads",
        remote,
        f"refs/heads/{target_branch}",
    )
    if remote_branch_check.returncode == 0:
        publication.error = f"Remote branch already exists: {remote}/{target_branch}"
        return publication
    if remote_branch_check.returncode not in {0, 2}:
        publication.error = _failure("Could not safely check remote branch existence", remote_branch_check)
        return publication

    switch = _git(working_root, "switch", "-c", target_branch, timeout=20)
    if switch.returncode != 0:
        publication.error = _failure("Could not create publication branch", switch)
        return publication

    stage = _git(working_root, "add", "--", *result.changes.paths, timeout=20)
    if stage.returncode != 0:
        publication.error = _failure("Could not stage reviewed changes", stage)
        return publication

    staged = _git(working_root, "diff", "--cached", "--quiet", "--", timeout=20)
    if staged.returncode == 0:
        publication.error = "No staged changes remained after verification"
        return publication
    if staged.returncode != 1:
        publication.error = _failure("Could not inspect staged changes", staged)
        return publication

    message = f"DevAgent: {result.task.goal.strip()}"
    if len(message) > 120:
        message = message[:117].rstrip() + "..."
    commit = _git(working_root, "commit", "-m", message, timeout=30)
    if commit.returncode != 0:
        publication.error = _failure("Git commit failed", commit)
        return publication

    head = _git(working_root, "rev-parse", "HEAD", timeout=10)
    if head.returncode != 0:
        publication.error = _failure("Could not resolve created commit", head)
        return publication
    publication.committed = True
    publication.commit = head.stdout.strip()

    push = _git(working_root, "push", "--set-upstream", remote, target_branch)
    if push.returncode != 0:
        publication.error = _failure("Git push failed", push)
        return publication

    publication.pushed = True
    return publication
