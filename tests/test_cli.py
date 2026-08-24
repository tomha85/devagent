from __future__ import annotations

from pathlib import Path

import pytest

from devagent.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert "0.2.0" in capsys.readouterr().out


def test_setup_and_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DEVAGENT_CONFIG", str(path))
    assert main(["setup", "--provider", "compatible", "--base-url", "http://127.0.0.1:11434/v1"]) == 0
    assert path.is_file()
    assert main(["doctor"]) == 0
    assert "DEVAGENT DOCTOR" in capsys.readouterr().out


def test_status_without_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert "No DevAgent runs" in capsys.readouterr().out

