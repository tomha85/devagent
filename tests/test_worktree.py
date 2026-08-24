from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devagent.worktree import select_worktree

from conftest import commit_all


def test_clean_repository_uses_external_detached_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    source = git_repo / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    original_head = commit_all(git_repo)

    selection = select_worktree(
        git_repo,
        "run-clean",
        enabled=True,
        git_head=original_head,
        dirty_files=[],
        state_root=tmp_path / "external-state",
    )

    assert selection.isolated
    assert not selection.creation_failed
    assert selection.root != git_repo
    assert not selection.root.is_relative_to(git_repo)
    assert (
        subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=selection.root,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    (selection.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (
        subprocess.run(
            ["git", "status", "--short"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == original_head
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=selection.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == original_head
    )


def test_dirty_repository_is_not_isolated(git_repo: Path, tmp_path: Path) -> None:
    target = git_repo / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    head = commit_all(git_repo)
    target.write_text("VALUE = 2\n", encoding="utf-8")

    selection = select_worktree(
        git_repo,
        "run-dirty",
        enabled=True,
        git_head=head,
        dirty_files=["app.py"],
        state_root=tmp_path / "state",
    )

    assert not selection.isolated
    assert not selection.creation_failed
    assert selection.root == git_repo
    assert "pre-existing changes" in selection.reason


def test_non_git_repository_uses_source_with_explicit_reason(tmp_path: Path) -> None:
    source = tmp_path / "plain"
    source.mkdir()

    selection = select_worktree(
        source,
        "run-plain",
        enabled=True,
        git_head=None,
        dirty_files=[],
        state_root=tmp_path / "state",
    )

    assert not selection.isolated
    assert not selection.creation_failed
    assert selection.root == source
    assert selection.reason == "repository is not a Git worktree"


def test_worktree_creation_failure_is_exact_and_fail_closed(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    head = commit_all(git_repo)
    real_run = subprocess.run

    def fake_run(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(argv, 128, "", "simulated worktree failure")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("devagent.worktree.subprocess.run", fake_run)
    selection = select_worktree(
        git_repo,
        "run-failure",
        enabled=True,
        git_head=head,
        dirty_files=[],
        state_root=tmp_path / "state",
    )

    assert not selection.isolated
    assert selection.creation_failed
    assert selection.root == git_repo
    assert selection.reason == "isolation unavailable: simulated worktree failure"
    assert (git_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
