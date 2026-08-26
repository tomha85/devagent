from __future__ import annotations

from pathlib import Path

import pytest

from devagent.plc.cli import _parser, main as plc_main


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="AnalysisOnly" TargetType="Controller">
  <Controller Use="Target" Name="AnalysisOnly" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main" MainRoutineName="Logic"><Routines><Routine Name="Logic" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
    </RLLContent></Routine></Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Main" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def test_public_plc_cli_is_analysis_and_fat_planning_only() -> None:
    help_text = _parser().format_help()

    assert "engineering review" in help_text.lower()
    assert "fat planning" in help_text.lower()
    assert "does not connect to or execute external plc software" in help_text.lower()
    assert "--rockwell-echo-runner" not in help_text
    assert "--rockwell-runtime-project" not in help_text
    assert "--rockwell-runtime-binding" not in help_text
    assert "--rockwell-time-quantum-us" not in help_text


def test_removed_direct_echo_flags_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        plc_main([str(project), "--rockwell-echo-runner", str(tmp_path / "runner")])

    assert exc.value.code == 2
