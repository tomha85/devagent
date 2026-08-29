from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.live import cli, commission


def _config(tmp_path: Path):
    security = SimpleNamespace(authentication_mode="ANONYMOUS", channel_summary="NONE")
    connection = SimpleNamespace(
        plc_id="line1",
        display_name="Line 1",
        endpoint="opc.tcp://127.0.0.1:4840/",
        security=security,
    )
    project = SimpleNamespace(metadata=SimpleNamespace(vendor="ROCKWELL"))
    spec = SimpleNamespace(
        connection=connection,
        engineering_project=project,
        required_tag_ids=("TAG:RUN",),
    )
    return commission.LoadedCommissioningConfig(
        source_path=tmp_path / "commission.json",
        source_sha256="abc123",
        specs=(spec,),
    )


def _result(*, all_complete: bool):
    return SimpleNamespace(
        all_complete=all_complete,
        started_at=SimpleNamespace(isoformat=lambda: "start"),
        finished_at=SimpleNamespace(isoformat=lambda: "finish"),
        plc_results={},
    )


def test_parser_accepts_commission_validate_and_output_dir(tmp_path) -> None:
    args = cli._build_parser().parse_args(
        [
            "commission",
            str(tmp_path / "commission.json"),
            "--validate-only",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert args.command == "commission"
    assert args.config == tmp_path / "commission.json"
    assert args.validate_only is True
    assert args.output_dir == tmp_path / "out"


def test_validate_only_never_runs_workflow(monkeypatch, tmp_path, capsys) -> None:
    loaded = _config(tmp_path)
    monkeypatch.setattr(commission, "load_commissioning_config", lambda path: loaded)

    async def forbidden(_config):
        raise AssertionError("workflow must not run during --validate-only")

    monkeypatch.setattr(commission, "run_loaded_commissioning_config", forbidden)
    args = cli._build_parser().parse_args(
        ["commission", str(tmp_path / "commission.json"), "--validate-only"]
    )
    assert asyncio.run(cli._run_commission(args)) == 0
    output = capsys.readouterr().out
    assert "Validation: PASS" in output
    assert "Network connection: NOT ATTEMPTED" in output


def test_commission_returns_zero_only_when_all_plcs_complete(monkeypatch, tmp_path) -> None:
    loaded = _config(tmp_path)
    monkeypatch.setattr(commission, "load_commissioning_config", lambda path: loaded)

    async def run_complete(_config):
        return _result(all_complete=True)

    monkeypatch.setattr(commission, "run_loaded_commissioning_config", run_complete)
    monkeypatch.setattr(
        commission,
        "commissioning_summary",
        lambda config, result: {
            "plcs": [
                {
                    "plc_id": "line1",
                    "state": "COMPLETE",
                    "connection_state": "CONNECTED",
                    "definitive_current_evidence": 1,
                    "excluded_raw_evidence": 0,
                    "limitations": [],
                    "error": None,
                }
            ]
        },
    )
    args = cli._build_parser().parse_args(
        ["commission", str(tmp_path / "commission.json")]
    )
    assert asyncio.run(cli._run_commission(args)) == 0

    async def run_limited(_config):
        return _result(all_complete=False)

    monkeypatch.setattr(commission, "run_loaded_commissioning_config", run_limited)
    assert asyncio.run(cli._run_commission(args)) == 2


def test_output_dir_calls_sanitized_artifact_writer(monkeypatch, tmp_path) -> None:
    loaded = _config(tmp_path)
    result = _result(all_complete=True)
    monkeypatch.setattr(commission, "load_commissioning_config", lambda path: loaded)

    async def run(_config):
        return result

    monkeypatch.setattr(commission, "run_loaded_commissioning_config", run)
    monkeypatch.setattr(
        commission,
        "commissioning_summary",
        lambda config, result: {
            "plcs": [
                {
                    "plc_id": "line1",
                    "state": "COMPLETE",
                    "connection_state": "CONNECTED",
                    "definitive_current_evidence": 1,
                    "excluded_raw_evidence": 0,
                    "limitations": [],
                    "error": None,
                }
            ]
        },
    )
    calls = []
    monkeypatch.setattr(
        commission,
        "write_commissioning_artifacts",
        lambda output, config, workflow_result: calls.append(
            (output, config, workflow_result)
        )
        or Path(output),
    )
    output = tmp_path / "out"
    args = cli._build_parser().parse_args(
        ["commission", str(tmp_path / "commission.json"), "--output-dir", str(output)]
    )
    assert asyncio.run(cli._run_commission(args)) == 0
    assert calls == [(output, loaded, result)]


def test_main_converts_filesystem_errors_to_cli_exit(monkeypatch, tmp_path) -> None:
    async def raise_oserror(args):
        raise FileNotFoundError("missing config")

    monkeypatch.setattr(cli, "_run", raise_oserror)
    with pytest.raises(SystemExit) as exc:
        cli.main(["commission", str(tmp_path / "missing.json")])
    assert exc.value.code == 2
