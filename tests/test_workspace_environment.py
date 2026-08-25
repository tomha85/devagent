from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devagent.artifacts import RunArtifacts
from devagent.workspace import Workspace


def _capture_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    captured: dict[str, Any] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: Any) -> SimpleNamespace:
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("devagent.workspace.subprocess.run", fake_run)
    artifacts = RunArtifacts(tmp_path, run_id="workspace-env-test")
    workspace = Workspace(tmp_path, artifacts)
    result = workspace.run(("python", "--version"), phase="qualification")

    assert result.passed
    return captured["env"]


def test_workspace_keeps_home_sandboxed_but_exposes_installed_rustup_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    developer_home = tmp_path / "developer-home"
    rustup_home = developer_home / ".rustup"
    rustup_home.mkdir(parents=True)
    cargo_home = developer_home / ".cargo"
    cargo_home.mkdir()

    monkeypatch.setenv("HOME", str(developer_home))
    monkeypatch.delenv("RUSTUP_HOME", raising=False)
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable-x86_64-unknown-linux-gnu")
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("CARGO_REGISTRIES_CRATES_IO_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    environment = _capture_environment(tmp_path, monkeypatch)

    assert environment["HOME"] != str(developer_home)
    assert environment["HOME"].endswith("/.devagent/runs/workspace-env-test/command-home")
    assert environment["RUSTUP_HOME"] == str(rustup_home)
    assert environment["RUSTUP_TOOLCHAIN"] == "stable-x86_64-unknown-linux-gnu"
    assert "CARGO_HOME" not in environment
    assert "CARGO_REGISTRIES_CRATES_IO_TOKEN" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_workspace_respects_explicit_rustup_home_without_exposing_cargo_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    developer_home = tmp_path / "developer-home"
    explicit_rustup = tmp_path / "shared-rustup"
    developer_home.mkdir()
    explicit_rustup.mkdir()

    monkeypatch.setenv("HOME", str(developer_home))
    monkeypatch.setenv("RUSTUP_HOME", str(explicit_rustup))
    monkeypatch.delenv("RUSTUP_TOOLCHAIN", raising=False)
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "secret-cargo"))
    monkeypatch.setenv("CARGO_REGISTRIES_PRIVATE_TOKEN", "must-not-leak")

    environment = _capture_environment(tmp_path, monkeypatch)

    assert environment["HOME"] != str(developer_home)
    assert environment["RUSTUP_HOME"] == str(explicit_rustup)
    assert "RUSTUP_TOOLCHAIN" not in environment
    assert "CARGO_HOME" not in environment
    assert "CARGO_REGISTRIES_PRIVATE_TOKEN" not in environment
