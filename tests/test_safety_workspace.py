from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devagent.artifacts import RunArtifacts
from devagent.safety import CommandPolicy, PathPolicy, SafetyError
from devagent.workspace import Workspace, classify_failure
from devagent.models import FailureClass
from devagent.orchestrator import _metrics

from conftest import commit_all


def workspace(root: Path, dirty: tuple[str, ...] = ()) -> Workspace:
    return Workspace(root, RunArtifacts(root), dirty)


def test_workspace_escape_is_blocked(tmp_path: Path) -> None:
    policy = PathPolicy(tmp_path)
    with pytest.raises(SafetyError, match="escapes"):
        policy.resolve("../outside.py")


@pytest.mark.parametrize("name", [".env", ".env.production", "server.pem", "id_rsa", "credentials.json", "secrets.yml"])
def test_secret_files_are_blocked(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_text("secret", encoding="utf-8")
    with pytest.raises(SafetyError, match="sensitive"):
        workspace(tmp_path).read_file(name)


def test_backup_precedes_edit_and_is_immutable(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    tools = workspace(tmp_path)
    tools.write_file("app.py", "new\n")
    tools.write_file("app.py", "newer\n")
    assert (tools.artifacts.backups / "app.py").read_text(encoding="utf-8") == "old\n"


def test_preexisting_dirty_file_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("developer work", encoding="utf-8")
    with pytest.raises(SafetyError, match="developer modification"):
        workspace(tmp_path, ("app.py",)).write_file("app.py", "agent work")


@pytest.mark.parametrize(
    "command",
    [
        ["git", "push"], ["git", "commit", "-m", "x"], ["git", "reset", "--hard"],
        ["rm", "-rf", "work"], ["sudo", "true"], ["curl", "https://example.com"],
        ["python", "-c", "print('x')"], ["python", "-m", "pip", "install", "x"],
        ["git", "-C", "/tmp", "push"], ["busybox", "rm", "file"], ["env"],
    ],
)
def test_command_policy_blocks_destructive_publish_and_escape_commands(command: list[str]) -> None:
    with pytest.raises(SafetyError):
        CommandPolicy.validate(command)


def test_command_runs_without_shell_and_uses_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "fails.py"
    script.write_text("print('success passed ok')\nraise SystemExit(7)\n", encoding="utf-8")
    result = workspace(tmp_path).run([sys.executable, "fails.py"], phase="test")
    assert result.exit_code == 7
    assert not result.passed


def test_success_is_invalidated_by_a_later_modification(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    tools = workspace(tmp_path)
    result = tools.run([sys.executable, "-m", "compileall", "-q", "."], phase="targeted")
    assert result.passed and result.revision == 0
    tools.replace_text("app.py", "1", "2")
    assert result.revision != tools.revision


def test_untracked_file_lines_count_toward_scope_gate(git_repo: Path) -> None:
    commit_all(git_repo)
    tools = workspace(git_repo)
    tools.write_file("generated.py", "\n".join(f"value_{index} = {index}" for index in range(20)) + "\n")
    metrics = _metrics(git_repo, tools.modified_paths)
    assert metrics.files_changed == 1
    assert metrics.lines_added == 20


@pytest.mark.parametrize(
    ("output", "expected"),
    [("AssertionError", FailureClass.ASSERTION_FAILURE), ("SyntaxError", FailureClass.SYNTAX_ERROR), ("No module named foo", FailureClass.IMPORT_ERROR)],
)
def test_failure_classification(output: str, expected: FailureClass) -> None:
    assert classify_failure(output, "") is expected
