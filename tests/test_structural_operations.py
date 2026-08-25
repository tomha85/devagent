from __future__ import annotations

from pathlib import Path

import pytest

from devagent.artifacts import RunArtifacts
from devagent.safety import SafetyError
from devagent.workspace import Workspace


def _workspace(tmp_path: Path, *, dirty_files: tuple[str, ...] = ()) -> tuple[Workspace, RunArtifacts]:
    artifacts = RunArtifacts(tmp_path, run_id="structural-ops")
    return Workspace(tmp_path, artifacts, dirty_files=dirty_files), artifacts


def test_delete_file_preserves_backup_and_records_modified_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    target = tmp_path / "legacy.py"
    target.write_text("legacy = True\n", encoding="utf-8")
    workspace, artifacts = _workspace(tmp_path)

    workspace.delete_file("legacy.py")

    assert not target.exists()
    assert workspace.modified_paths == {"legacy.py"}
    assert (artifacts.backup_dir / "legacy.py").read_text(encoding="utf-8") == "legacy = True\n"


def test_move_file_preserves_backup_and_refuses_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    source = tmp_path / "old" / "service.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    workspace, artifacts = _workspace(tmp_path)

    workspace.move_file("old/service.py", "new/service.py")

    assert not source.exists()
    assert (tmp_path / "new" / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert workspace.modified_paths == {"old/service.py", "new/service.py"}
    assert (artifacts.backup_dir / "old" / "service.py").is_file()

    collision = tmp_path / "collision.py"
    collision.write_text("keep\n", encoding="utf-8")
    with pytest.raises(SafetyError, match="overwrite structural destination"):
        workspace.move_file("new/service.py", "collision.py")


def test_rename_file_uses_same_safe_move_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    (tmp_path / "before.txt").write_text("payload\n", encoding="utf-8")
    workspace, _artifacts = _workspace(tmp_path)

    workspace.rename_file("before.txt", "after.txt")

    assert not (tmp_path / "before.txt").exists()
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "payload\n"


def test_structural_operations_protect_dirty_files_and_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    (tmp_path / "dirty.py").write_text("developer change\n", encoding="utf-8")
    (tmp_path / "safe.py").write_text("safe\n", encoding="utf-8")
    workspace, _artifacts = _workspace(tmp_path, dirty_files=("dirty.py",))

    with pytest.raises(SafetyError, match="developer modification"):
        workspace.delete_file("dirty.py")
    with pytest.raises(SafetyError, match="developer modification"):
        workspace.move_file("safe.py", "dirty.py")
    with pytest.raises(SafetyError, match="escapes workspace"):
        workspace.move_file("safe.py", "../escape.py")


def test_structural_operations_reject_symlinked_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    real = tmp_path / "real"
    real.mkdir()
    (real / "source.txt").write_text("payload\n", encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    workspace, _artifacts = _workspace(tmp_path)

    with pytest.raises(SafetyError, match="do not follow symlinks"):
        workspace.delete_file("linked/source.txt")
    with pytest.raises(SafetyError, match="do not follow symlinks"):
        workspace.move_file("real/source.txt", "linked/destination.txt")



def test_structural_operations_reject_directories_and_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    directory = tmp_path / "directory"
    directory.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    workspace, _artifacts = _workspace(tmp_path)

    with pytest.raises(SafetyError, match="regular file"):
        workspace.delete_file("directory")
    with pytest.raises(SafetyError, match="do not follow symlinks"):
        workspace.delete_file("link.txt")
