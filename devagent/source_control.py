from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devagent.models import Outcome, RunResult, SourceControlResult
from devagent.safety import is_secret_path


_PROTECTED_BRANCHES = {"main", "master", "trunk"}
_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class PublicationPlan:
    """Deterministic source-control context captured before the engineering run."""

    mode: str
    remote: str
    branch: str | None
    base_commit: str
    expected_remote_head: str | None


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


def _remote_branch_head(root: Path, remote: str, branch: str) -> str | None:
    completed = _git(
        root,
        "ls-remote",
        "--exit-code",
        "--heads",
        remote,
        f"refs/heads/{branch}",
        timeout=30,
    )
    if completed.returncode == 2:
        return None
    if completed.returncode != 0:
        raise ValueError(_failure("Could not safely inspect remote branch", completed))
    line = next((line for line in completed.stdout.splitlines() if line.strip()), "")
    if not line:
        raise ValueError("Remote branch lookup succeeded without returning a commit")
    return line.split()[0]


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = _git(root, "merge-base", "--is-ancestor", ancestor, descendant, timeout=10)
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise ValueError(_failure("Could not compare local and remote branch history", completed))


def prepare_publication(
    root: Path | str,
    *,
    explicit_branch: str | None = None,
    remote: str = "origin",
) -> PublicationPlan:
    """Resolve the safe publication target and exact worktree base before model execution."""

    repository_root = Path(root).expanduser().resolve()
    if not repository_root.is_dir():
        raise ValueError(f"Repository does not exist: {repository_root}")
    if not _SAFE_REMOTE.fullmatch(remote):
        raise ValueError("Remote name contains unsupported characters")

    local_head_result = _git(repository_root, "rev-parse", "HEAD", timeout=10)
    if local_head_result.returncode != 0:
        raise ValueError(_failure("Could not resolve local Git HEAD", local_head_result))
    local_head = local_head_result.stdout.strip()

    remote_check = _git(repository_root, "remote", "get-url", remote, timeout=10)
    if remote_check.returncode != 0:
        raise ValueError(_failure(f"Git remote {remote!r} is unavailable", remote_check))

    current_result = _git(repository_root, "branch", "--show-current", timeout=10)
    current_branch = current_result.stdout.strip() if current_result.returncode == 0 else ""

    if explicit_branch:
        branch_check = _git(repository_root, "check-ref-format", "--branch", explicit_branch, timeout=10)
        if branch_check.returncode != 0:
            raise ValueError(_failure("Invalid branch name", branch_check))
        if explicit_branch.lower() in _PROTECTED_BRANCHES:
            raise ValueError(f"Refusing to publish directly to protected branch: {explicit_branch}")
        return PublicationPlan(
            mode="new",
            remote=remote,
            branch=explicit_branch,
            base_commit=local_head,
            expected_remote_head=None,
        )

    if not current_branch or current_branch.lower() in _PROTECTED_BRANCHES:
        return PublicationPlan(
            mode="new",
            remote=remote,
            branch=None,
            base_commit=local_head,
            expected_remote_head=None,
        )

    branch_check = _git(repository_root, "check-ref-format", "--branch", current_branch, timeout=10)
    if branch_check.returncode != 0:
        raise ValueError(_failure("Invalid current branch name", branch_check))

    remote_head = _remote_branch_head(repository_root, remote, current_branch)
    base_commit = local_head
    if remote_head is not None:
        fetch = _git(
            repository_root,
            "fetch",
            "--no-tags",
            remote,
            f"refs/heads/{current_branch}",
            timeout=60,
        )
        if fetch.returncode != 0:
            raise ValueError(_failure("Could not fetch current development branch", fetch))
        fetched = _git(repository_root, "rev-parse", "FETCH_HEAD", timeout=10)
        if fetched.returncode != 0 or fetched.stdout.strip() != remote_head:
            raise ValueError("Fetched development branch does not match the inspected remote HEAD")

        if local_head == remote_head:
            base_commit = local_head
        elif _is_ancestor(repository_root, local_head, remote_head):
            base_commit = remote_head
        elif _is_ancestor(repository_root, remote_head, local_head):
            base_commit = local_head
        else:
            raise ValueError(
                f"Local and remote branch histories diverged for {current_branch}; "
                "resolve the branch manually before running DevAgent"
            )

    return PublicationPlan(
        mode="continue",
        remote=remote,
        branch=current_branch,
        base_commit=base_commit,
        expected_remote_head=remote_head,
    )


def publish_verified_branch(
    result: RunResult,
    *,
    branch: str | None = None,
    remote: str = "origin",
    mode: str = "new",
    expected_remote_head: str | None = None,
) -> SourceControlResult:
    """Commit and push a VERIFIED result without PR, merge, rebase, or force push."""

    target_branch = branch or f"devagent/{result.run_id}"
    publication = SourceControlResult(requested=True, remote=remote, branch=target_branch)

    if result.outcome is not Outcome.VERIFIED:
        publication.error = "Branch publishing is allowed only for VERIFIED runs"
        return publication
    if mode not in {"new", "continue"}:
        publication.error = f"Unsupported publication mode: {mode}"
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

    if mode == "new":
        local_check = _git(working_root, "show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}", timeout=10)
        if local_check.returncode == 0:
            publication.error = f"Local branch already exists: {target_branch}"
            return publication
        try:
            remote_head = _remote_branch_head(working_root, remote, target_branch)
        except ValueError as exc:
            publication.error = str(exc)
            return publication
        if remote_head is not None:
            publication.error = f"Remote branch already exists: {remote}/{target_branch}"
            return publication
        switch = _git(working_root, "switch", "-c", target_branch, timeout=20)
        if switch.returncode != 0:
            publication.error = _failure("Could not create publication branch", switch)
            return publication
    else:
        if result.repository.git_branch != target_branch:
            publication.error = (
                "Continuation target must match the developer's current local branch: "
                f"{result.repository.git_branch or '(detached)'} != {target_branch}"
            )
            return publication
        try:
            remote_head = _remote_branch_head(working_root, remote, target_branch)
        except ValueError as exc:
            publication.error = str(exc)
            return publication
        if remote_head != expected_remote_head:
            publication.error = (
                f"Remote branch changed during run: expected {expected_remote_head or '(absent)'}, "
                f"found {remote_head or '(absent)'}"
            )
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

    if mode == "continue":
        try:
            remote_head = _remote_branch_head(working_root, remote, target_branch)
        except ValueError as exc:
            publication.error = str(exc)
            return publication
        if remote_head != expected_remote_head:
            publication.error = (
                f"Remote branch changed before push: expected {expected_remote_head or '(absent)'}, "
                f"found {remote_head or '(absent)'}"
            )
            return publication
        push = _git(working_root, "push", remote, f"HEAD:refs/heads/{target_branch}")
    else:
        push = _git(working_root, "push", "--set-upstream", remote, target_branch)
    if push.returncode != 0:
        publication.error = _failure("Git push failed", push)
        return publication

    publication.pushed = True
    return publication
