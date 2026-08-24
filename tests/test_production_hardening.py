from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conftest import commit_all
from devagent.discovery import discover_repository
from devagent.orchestrator import _command_kind
from devagent.worktree import WorktreeSelection, select_worktree


def _committed_repository(root: Path) -> str:
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return commit_all(root)


def _select(
    root: Path, state_root: Path, run_id: str
) -> tuple[list[str], WorktreeSelection]:
    repository = discover_repository(root, probe_capabilities=False)
    return repository.dirty_files, select_worktree(
        root,
        run_id,
        enabled=True,
        git_head=repository.git_head,
        dirty_files=repository.dirty_files,
        state_root=state_root,
    )


def test_generated_only_untracked_state_preserves_worktree_isolation(
    git_repo: Path, tmp_path: Path
) -> None:
    _committed_repository(git_repo)
    (git_repo / ".devagent" / "memory").mkdir(parents=True)
    (git_repo / ".devagent" / "memory" / "repository.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"cache")
    (git_repo / "pkg" / "__pycache__").mkdir(parents=True)
    (git_repo / "pkg" / "__pycache__" / "module.pyc").write_bytes(b"cache")
    (git_repo / ".pytest_cache").mkdir()
    (git_repo / ".pytest_cache" / "README.md").write_text(
        "cache\n", encoding="utf-8"
    )
    (git_repo / "standalone.pyc").write_bytes(b"cache")

    dirty_files, selection = _select(
        git_repo, tmp_path / "generated-state", "generated-only"
    )

    assert dirty_files == []
    assert selection.isolated


def test_real_untracked_source_remains_protected(
    git_repo: Path, tmp_path: Path
) -> None:
    _committed_repository(git_repo)
    (git_repo / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")

    dirty_files, selection = _select(
        git_repo, tmp_path / "untracked-state", "real-untracked"
    )

    assert dirty_files == ["new_module.py"]
    assert not selection.isolated
    assert "dirty files remain protected" in selection.reason


def test_modified_tracked_file_remains_protected(
    git_repo: Path, tmp_path: Path
) -> None:
    _committed_repository(git_repo)
    (git_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    dirty_files, selection = _select(
        git_repo, tmp_path / "modified-state", "tracked-modification"
    )

    assert dirty_files == ["app.py"]
    assert not selection.isolated
    assert "dirty files remain protected" in selection.reason


def test_builtin_git_verification_is_an_exact_git_only_allowlist() -> None:
    git_repository = SimpleNamespace(
        git_head="abc123", capabilities=[], components=[]
    )
    plain_repository = SimpleNamespace(
        git_head=None, capabilities=[], components=[]
    )

    assert _command_kind(("git", "diff", "--check"), git_repository) == "harness"
    assert _command_kind(("git", "status", "--short"), git_repository) is None
    assert _command_kind(("git", "diff", "--check", "app.py"), git_repository) is None
    assert _command_kind(("git", "diff", "--check"), plain_repository) is None
