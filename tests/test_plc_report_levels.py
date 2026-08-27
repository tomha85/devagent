from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from devagent.plc.cli import main as plc_main


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ReportLevels" TargetType="Controller">
  <Controller Use="Target" Name="ReportLevels" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="Ready" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(Run)OTE(Ready);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Main" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _write_project(tmp_path: Path) -> Path:
    path = tmp_path / "ReportLevels.L5X"
    path.write_text(PROJECT, encoding="utf-8")
    return path


def test_default_console_is_concise_and_full_detail_is_artifactized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _write_project(tmp_path)
    output = tmp_path / "run"

    assert plc_main([str(project), "--output-dir", str(output)]) == 2
    stdout = capsys.readouterr().out

    assert "DEVAGENT PLC ENGINEERING REVIEW" in stdout
    assert "REPORT LEVELS" in stdout
    assert "FAT assertions generated:" in stdout
    assert "Engineer FAT scenario groups:" in stdout
    assert "Use --full-report" in stdout
    assert "TOP ENGINEERING RISKS" in stdout
    assert "TOP FINDINGS" not in stdout
    assert "Classification:" in stdout
    assert "Why:" in stdout
    assert "Impact:" in stdout
    assert "Recommended Action:" in stdout
    assert "## FAT Test Plan and Execution" not in stdout
    assert "## Risk Detection" not in stdout

    expected_new = {
        "report_summary.txt",
        "fat_scenarios.md",
        "fat_scenario_index.json",
        "fat_tests.csv",
        "risk_register.csv",
        "optimization_report.md",
    }
    assert expected_new <= {path.name for path in output.iterdir()}

    fat_tests = json.loads((output / "fat_tests.json").read_text(encoding="utf-8"))
    scenario_index = json.loads((output / "fat_scenario_index.json").read_text(encoding="utf-8"))
    mapped_ids = [test_id for scenario in scenario_index for test_id in scenario["test_ids"]]
    expected_ids = [test["id"] for test in fat_tests]
    assert sorted(mapped_ids) == sorted(expected_ids)
    assert len(mapped_ids) == len(set(mapped_ids))

    with (output / "fat_tests.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(fat_tests)
    assert {row["test_id"] for row in rows} == set(expected_ids)

    scenarios = (output / "fat_scenarios.md").read_text(encoding="utf-8")
    assert "Low-level FAT assertions preserved" in scenarios
    assert "fat_scenario_index.json" in scenarios
    assert "fat_tests.csv" in scenarios

    summary = (output / "report_summary.txt").read_text(encoding="utf-8")
    assert "TOP ENGINEERING RISKS" in summary
    assert "Recommended Action:" in summary

    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["report_levels"]["level_1"] == ["report_summary.txt"]
    assert "fat_scenarios.md" in manifest["report_levels"]["level_2"]
    assert "evidence_manifest.json" in manifest["report_levels"]["level_3"]

    # The terminal view must not leak the low-level assertion dump by default.
    if expected_ids:
        assert expected_ids[0] not in stdout


def test_full_report_flag_restores_complete_terminal_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _write_project(tmp_path)

    assert plc_main([str(project), "--no-write", "--full-report"]) == 2
    stdout = capsys.readouterr().out

    assert "## FAT Test Plan and Execution" in stdout
    assert "## Risk Detection" in stdout
    assert "## Release Readiness" in stdout
    assert "DEVAGENT PLC ENGINEERING REVIEW" not in stdout
