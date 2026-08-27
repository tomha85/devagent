from __future__ import annotations

from pathlib import Path

import pytest

import devagent.plc.cli as plc_cli
from devagent.plc.production_models import StageRecord, StageStatus, capture_stage_progress


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Progress" TargetType="Controller">
  <Controller Use="Target" Name="Progress" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Main" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _write_project(tmp_path: Path) -> Path:
    path = tmp_path / "Progress.L5X"
    path.write_text(PROJECT, encoding="utf-8")
    return path


def test_stage_progress_observer_is_scoped() -> None:
    observed: list[StageRecord] = []

    with capture_stage_progress(observed.append):
        first = StageRecord(1, "PROJECT VALIDATION", StageStatus.PASS, "inside")

    StageRecord(2, "CANONICAL PLC IR", StageStatus.PASS, "outside")

    assert observed == [first]


def test_cli_prints_progress_before_pipeline_execution(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _write_project(tmp_path)
    original = plc_cli.run_production_verification_v5
    entered = False

    def wrapped(*args, **kwargs):
        nonlocal entered
        entered = True
        early = capsys.readouterr().out
        assert "DevAgent PLC is working..." in early
        assert f"Project: {project.resolve()}" in early
        assert "[ 1/15] PROJECT VALIDATION" in early
        return original(*args, **kwargs)

    monkeypatch.setattr(plc_cli, "run_production_verification_v5", wrapped)

    assert plc_cli.main([str(project), "--no-write"]) == 2
    assert entered is True
    stdout = capsys.readouterr().out
    assert "[ 2/15] CANONICAL PLC IR" in stdout
    assert "[15/15] RELEASE READINESS" in stdout
    assert "Final stage results:" in stdout


def test_cli_verbose_prints_stage_summaries_and_v5_finalization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _write_project(tmp_path)

    assert plc_cli.main([str(project), "--no-write", "--verbose"]) == 2
    stdout = capsys.readouterr().out

    assert "[ 1/15] PROJECT VALIDATION" in stdout
    assert "      -> PASS: Validated Rockwell full-project L5X" in stdout
    assert "Canonical IR:" in stdout
    assert "-> FINALIZED" in stdout
    assert "Final stage results:" in stdout
