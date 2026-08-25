from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devagent.artifacts import RunArtifacts
from devagent.runtime import NetworkMode, RuntimeExecutor, RuntimePolicy, RuntimePolicyError, SandboxMode
from devagent.safety import CommandPolicy, SafetyError
from devagent.workspace import Workspace


def test_linux_bwrap_network_is_denied_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devagent.runtime.sys.platform", "linux")
    monkeypatch.setattr("devagent.runtime.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    runtime = RuntimeExecutor(tmp_path, RuntimePolicy(sandbox=SandboxMode.AUTO, network=NetworkMode.DENY))

    argv = runtime.prepare(("python", "--version"))

    assert argv[0] == "/usr/bin/bwrap"
    assert "--ro-bind" in argv
    assert "--bind" in argv
    assert "--unshare-net" in argv
    assert argv[-2:] == ("python", "--version")


def test_linux_bwrap_can_explicitly_inherit_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devagent.runtime.sys.platform", "linux")
    monkeypatch.setattr("devagent.runtime.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    runtime = RuntimeExecutor(tmp_path, RuntimePolicy(sandbox=SandboxMode.AUTO, network=NetworkMode.INHERIT))

    argv = runtime.prepare(("pytest", "-q"))

    assert "--unshare-net" not in argv
    assert argv[-2:] == ("pytest", "-q")


def test_linux_bwrap_rebinds_external_run_state_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    run_home = tmp_path / "source" / ".devagent" / "runs" / "run-1" / "command-home"
    worktree.mkdir()
    run_home.mkdir(parents=True)
    monkeypatch.setattr("devagent.runtime.sys.platform", "linux")
    monkeypatch.setattr("devagent.runtime.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    runtime = RuntimeExecutor(worktree, RuntimePolicy(sandbox=SandboxMode.AUTO))

    argv = runtime.prepare(("pytest", "-q"), writable_paths=(run_home,))

    bind_triplets = [argv[index:index + 3] for index, token in enumerate(argv) if token == "--bind"]
    assert ("--bind", str(worktree.resolve()), str(worktree.resolve())) in bind_triplets
    assert ("--bind", str(run_home.resolve()), str(run_home.resolve())) in bind_triplets


def test_required_sandbox_fails_closed_when_bwrap_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devagent.runtime.sys.platform", "linux")
    monkeypatch.setattr("devagent.runtime.shutil.which", lambda name: None)
    runtime = RuntimeExecutor(tmp_path, RuntimePolicy(sandbox=SandboxMode.REQUIRED))

    with pytest.raises(RuntimePolicyError, match="required"):
        runtime.prepare(("pytest", "-q"))


def test_safe_pip_install_requires_requirement_file() -> None:
    with pytest.raises(SafetyError, match="requires -r"):
        CommandPolicy.validate(("pip", "install", "requests"), allow_dependency_install=True)

    normalized = CommandPolicy.validate(
        ("pip", "install", "-r", "requirements.txt"),
        allow_dependency_install=True,
    )
    assert normalized[:4] == ("pip", "install", "-r", "requirements.txt")
    assert "--no-input" in normalized
    assert "--disable-pip-version-check" in normalized


def test_safe_node_install_requires_lockfile_preserving_command() -> None:
    with pytest.raises(SafetyError, match="npm ci"):
        CommandPolicy.validate(("npm", "install"), allow_dependency_install=True)

    normalized = CommandPolicy.validate(("npm", "ci"), allow_dependency_install=True)
    assert normalized == ("npm", "ci", "--ignore-scripts")


def test_workspace_dependency_install_requires_explicit_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    monkeypatch.setenv("DEVAGENT_ALLOW_DEPENDENCY_INSTALL", "1")
    monkeypatch.setenv("DEVAGENT_NETWORK", "deny")
    tools = Workspace(tmp_path, RunArtifacts(tmp_path, run_id="dependency-network-deny"))

    with pytest.raises(SafetyError, match="DEVAGENT_NETWORK=inherit"):
        tools.run(("pip", "install", "-r", "requirements.txt"), phase="dependency")


def test_workspace_runs_normalized_dependency_install_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    monkeypatch.setenv("DEVAGENT_ALLOW_DEPENDENCY_INSTALL", "1")
    monkeypatch.setenv("DEVAGENT_NETWORK", "inherit")
    captured: dict[str, Any] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: Any) -> SimpleNamespace:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("devagent.workspace.subprocess.run", fake_run)
    tools = Workspace(tmp_path, RunArtifacts(tmp_path, run_id="dependency-install"))
    result = tools.run(("pip", "install", "-r", "requirements.txt"), phase="dependency")

    assert result.passed
    assert captured["argv"][:4] == ("pip", "install", "-r", "requirements.txt")
    assert "--no-input" in captured["argv"]
    assert captured["env"]["DEVAGENT_NETWORK_MODE"] == "inherit"
