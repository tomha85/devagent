from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import devagent.live as live
from devagent.live import cli, plan as plan_module
from devagent.live.plan import LivePlanReferenceStatus


def _plan(*, complete: bool):
    unresolved = ()
    required = ("TAG:RUN",)
    if not complete:
        unresolved = (
            SimpleNamespace(
                reference="RunCmd",
                status=LivePlanReferenceStatus.AMBIGUOUS,
                reason="multiple exact candidates",
            ),
        )
        required = ()
    return SimpleNamespace(
        complete=complete,
        engineering_project_path=Path("/tmp/project.L5X"),
        vendor="ROCKWELL",
        plc_id="line1",
        plc_name="Line 1",
        endpoint="opc.tcp://127.0.0.1:4840/",
        references=(SimpleNamespace(),),
        required_tag_ids=required,
        unresolved=unresolved,
    )


def test_parser_accepts_plan_command() -> None:
    args = cli._build_parser().parse_args(
        [
            "plan",
            "project.L5X",
            "--plc-id",
            "line1",
            "--plc-name",
            "Line 1",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--output",
            "commission.json",
        ]
    )
    assert args.command == "plan"
    assert args.project == Path("project.L5X")
    assert args.plc_id == "line1"
    assert args.plc_name == "Line 1"
    assert args.output == Path("commission.json")


def test_complete_plan_writes_config_without_opening_opcua(monkeypatch, tmp_path, capsys) -> None:
    generated = _plan(complete=True)
    calls = []
    monkeypatch.setattr(
        plan_module,
        "analyze_and_build_live_commission_plan",
        lambda *args, **kwargs: generated,
    )
    monkeypatch.setattr(
        plan_module,
        "write_live_commission_plan",
        lambda output, supplied: calls.append((output, supplied))
        or (Path(output), Path(str(output) + ".plan.json")),
    )

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("plan command must not open OPC UA clients")

    monkeypatch.setattr(cli, "ReadOnlyOpcUaClient", ForbiddenClient)
    output = tmp_path / "commission.json"
    args = cli._build_parser().parse_args(
        [
            "plan",
            "project.L5X",
            "--plc-id",
            "line1",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--output",
            str(output),
        ]
    )
    assert asyncio.run(cli._run_plan(args)) == 0
    assert calls == [(output, generated)]
    text = capsys.readouterr().out
    assert "Plan: COMPLETE" in text
    assert "--validate-only" in text


def test_incomplete_plan_returns_two_and_does_not_write(monkeypatch, tmp_path, capsys) -> None:
    generated = _plan(complete=False)
    monkeypatch.setattr(
        plan_module,
        "analyze_and_build_live_commission_plan",
        lambda *args, **kwargs: generated,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("incomplete plan must not write an executable config")

    monkeypatch.setattr(plan_module, "write_live_commission_plan", forbidden)
    args = cli._build_parser().parse_args(
        [
            "plan",
            "project.L5X",
            "--plc-id",
            "line1",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--output",
            str(tmp_path / "commission.json"),
        ]
    )
    assert asyncio.run(cli._run_plan(args)) == 2
    text = capsys.readouterr().out
    assert "AMBIGUOUS" in text
    assert "config was not written" in text


def test_plan_api_is_exported_from_devagent_live() -> None:
    for name in (
        "LiveCommissionPlan",
        "LivePlanReference",
        "LivePlanReferenceStatus",
        "analyze_and_build_live_commission_plan",
        "build_live_commission_plan",
        "write_live_commission_plan",
    ):
        assert name in live.__all__
        assert getattr(live, name) is not None


def test_plan_public_surface_has_no_control_operations() -> None:
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    ):
        assert not hasattr(live.LiveCommissionPlan, prohibited)
