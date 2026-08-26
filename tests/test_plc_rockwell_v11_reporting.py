from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest
from devagent.plc.semantic_coverage_report import render_semantic_coverage_section


def _write(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="BrainReport" TargetType="Controller">
  <Controller Name="BrainReport" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Step" TagType="Base" DataType="DINT" />
      <Tag Name="Axis" TagType="Base" DataType="AXIS_CIP_DRIVE" />
      <Tag Name="Jog" TagType="Base" DataType="MOTION_INSTRUCTION" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Enable)EQU(Step,1)MOV(2,Step);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(Enable)MAJ(Axis,Jog,1,100.0);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "BrainReport.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v11_report_exposes_brain_owned_state_and_motion_runtime_contracts(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write(tmp_path))
    manifest = build_semantic_coverage_manifest(result.project)
    report = render_semantic_coverage_section(result.project)

    assert manifest["schema"] == "devagent-plc-semantic-coverage-v1"
    assert manifest["motion_runtime_semantics"]["modeled_occurrences"] == 1
    assert manifest["motion_runtime_semantics"]["requires_qualified_runtime_evidence"] is True
    assert manifest["state_machine_semantics"]["transition_count"] == 1
    assert manifest["state_machine_semantics"]["state_tag_count"] == 1

    assert "Motion runtime contracts: **1**" in report
    assert "Motion runtime evidence required: **yes**" in report
    assert "Discovered state transitions: **1** across **1** state tag(s)" in report

    runtime_tests = [
        item for item in result.fat_tests
        if item.scenario in {"MOTION_RUNTIME", "STATE_TRANSITION_RUNTIME"}
    ]
    assert len(runtime_tests) == 2
    assert all(item.execution_status == "NOT_RUN" for item in runtime_tests)
