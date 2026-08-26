from __future__ import annotations

from pathlib import Path

from devagent.plc.analysis import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus


def _write(tmp_path: Path, body: str, *, tags: str = "", aoi: str = "<AddOnInstructionDefinitions />") -> Path:
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V2" TargetType="Controller">
  <Controller Name="V2" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    {aoi}
    <Tags>{tags}</Tags>
    <Programs><Program Name="MainProgram"><Routines>{body}</Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" /></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V2.L5X"
    path.write_text(content, encoding="utf-8")
    return path


def test_v2_models_parallel_branch_without_cross_output_dependencies(tmp_path: Path) -> None:
    body = '''<Routine Name="Main" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[[XIC(A)OTE(X),XIC(B)OTE(Y)];]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />' for name in ("A", "B", "X", "Y"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    deps = {(e.source, e.target) for e in result.graph.edges if e.kind == "DEPENDS_ON"}
    assert ("X", "A") in deps
    assert ("Y", "B") in deps
    assert ("X", "B") not in deps
    assert ("Y", "A") not in deps
    assert result.project.branch_rung_semantic_count == 1
    assert result.project.branch_rung_total == 1
    branch = next(c for c in result.static_checks if c.id == "BRANCH_DEPENDENCY_SEMANTICS")
    assert branch.status is StaticCheckStatus.PASS


def test_v2_nested_branch_generates_positive_paths_and_blocking_case(tmp_path: Path) -> None:
    body = '''<Routine Name="Main" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[XIC(A)[XIC(B),XIC(C)]OTE(O);]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />' for name in ("A", "B", "C", "O"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    positives = [t for t in result.fat_tests if t.output_tag == "O" and t.scenario == "POSITIVE_PATH"]
    assert {tuple(t.preconditions.items()) for t in positives} == {
        (("A", True), ("B", True)),
        (("A", True), ("C", True)),
    }
    assert any(t.output_tag == "O" and t.scenario == "NEGATIVE_BRANCH" for t in result.fat_tests)


def test_v2_normalizes_st_assignment_and_control_dependencies(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[IF StartPB THEN]]></Line>
      <Line Number="1"><![CDATA[MotorRun := Ready AND NOT Fault;]]></Line>
      <Line Number="2"><![CDATA[END_IF;]]></Line>
    </STContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />' for name in ("StartPB", "Ready", "Fault", "MotorRun"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert result.project.st_statement_total == 3
    assert result.project.st_statement_semantic_count == 3
    assignment = next(s for s in result.project.logic_statements if "MotorRun :=" in s.text)
    assert assignment.semantic_state is PLCSemanticState.FULL
    assert assignment.writes == ("MotorRun",)
    assert set(assignment.reads) == {"StartPB", "Ready", "Fault"}
    deps = {(e.source, e.target) for e in result.graph.edges if e.kind == "DEPENDS_ON"}
    assert {("MotorRun", "StartPB"), ("MotorRun", "Ready"), ("MotorRun", "Fault")} <= deps


def test_v2_normalizes_aoi_body_and_proven_call_interface(tmp_path: Path) -> None:
    aoi = '''<AddOnInstructionDefinitions><AddOnInstructionDefinition Name="MotorAOI">
      <Parameters>
        <Parameter Name="EnableIn" Usage="Input" DataType="BOOL" />
        <Parameter Name="EnableOut" Usage="Output" DataType="BOOL" />
        <Parameter Name="Start" Usage="Input" DataType="BOOL" Required="true" Visible="true" />
        <Parameter Name="Run" Usage="Output" DataType="BOOL" Required="true" Visible="true" />
      </Parameters>
      <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine></Routines>
    </AddOnInstructionDefinition></AddOnInstructionDefinitions>'''
    body = '''<Routine Name="Main" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[MotorAOI(Motor1,StartPB,MotorRun);]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = '''
      <Tag Name="Motor1" TagType="Base" DataType="MotorAOI" />
      <Tag Name="StartPB" TagType="Base" DataType="BOOL" />
      <Tag Name="MotorRun" TagType="Base" DataType="BOOL" />'''
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags, aoi=aoi))

    assert result.project.aoi_internal_modeled_count == 1
    assert result.project.aoi_call_total == 1
    assert result.project.aoi_call_bound_count == 1
    rung = result.project.rungs[0]
    assert {"Motor1", "StartPB"} <= set(rung.reads)
    assert {"Motor1", "MotorRun"} <= set(rung.writes)
    deps = {(e.source, e.target) for e in result.graph.edges if e.kind == "DEPENDS_ON"}
    assert ("MotorRun", "StartPB") in deps
    assert any(t.output_tag == "MotorRun" for t in result.fat_tests)


def test_v2_does_not_bind_aoi_without_proven_backing_tag(tmp_path: Path) -> None:
    aoi = '''<AddOnInstructionDefinitions><AddOnInstructionDefinition Name="MotorAOI">
      <Parameters><Parameter Name="Start" Usage="Input" DataType="BOOL" Required="true" />
      <Parameter Name="Run" Usage="Output" DataType="BOOL" Required="true" /></Parameters>
      <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine></Routines>
    </AddOnInstructionDefinition></AddOnInstructionDefinitions>'''
    body = '''<Routine Name="Main" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[MotorAOI(NotAnInstance,StartPB,MotorRun);]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />' for name in ("NotAnInstance", "StartPB", "MotorRun"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags, aoi=aoi))

    assert result.project.aoi_call_total == 1
    assert result.project.aoi_call_bound_count == 0
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    call = next(c for c in result.static_checks if c.id == "AOI_CALL_BINDING")
    assert call.status is StaticCheckStatus.WARN


def test_v2_recognizes_motion_instructions_without_overclaiming_direction(tmp_path: Path) -> None:
    body = '''<Routine Name="Motion" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[MAJ(Axis1,JogControl,1,100.0);]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = '<Tag Name="Axis1" TagType="Base" DataType="AXIS_CIP_DRIVE" /><Tag Name="JogControl" TagType="Base" DataType="MOTION_INSTRUCTION" />'
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert result.project.unknown_instruction_names == []
    assert result.project.partially_modeled_instruction_names == ["MAJ"]
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any("directionally PARTIAL" in item for item in result.limitations)
