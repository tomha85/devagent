from __future__ import annotations

from pathlib import Path

from devagent.plc.analysis import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus


def _write(tmp_path: Path, body: str, *, tags: str = "", aoi: str = "<AddOnInstructionDefinitions />") -> Path:
    main_routine = body.split('<Routine Name="', 1)[1].split('"', 1)[0]
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V2" TargetType="Controller">
  <Controller Name="V2" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    {aoi}
    <Tags>{tags}</Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="{main_routine}"><Routines>{body}</Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
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


def test_v2_elsif_branch_remains_partial_until_full_boolean_ast_exists(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[IF A THEN]]></Line>
      <Line Number="1"><![CDATA[X := B;]]></Line>
      <Line Number="2"><![CDATA[ELSIF C THEN]]></Line>
      <Line Number="3"><![CDATA[X := D;]]></Line>
      <Line Number="4"><![CDATA[END_IF;]]></Line>
    </STContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />' for name in ("A", "B", "C", "D", "X"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    elsif = next(s for s in result.project.logic_statements if s.text.startswith("ELSIF"))
    branch_write = next(s for s in result.project.logic_statements if "X := D" in s.text)
    assert elsif.semantic_state is PLCSemanticState.PARTIAL
    assert branch_write.semantic_state is PLCSemanticState.PARTIAL
    assert {"C", "D"} <= set(branch_write.reads)


def test_v2_st_literals_do_not_become_fake_tag_dependencies(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[Message := 'Motor Ready';]]></Line>
      <Line Number="1"><![CDATA[Delay := T#5s;]]></Line>
    </STContent></Routine>'''
    tags = '<Tag Name="Message" TagType="Base" DataType="STRING" /><Tag Name="Delay" TagType="Base" DataType="TIME" />'
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    message = next(s for s in result.project.logic_statements if s.text.startswith("Message"))
    delay = next(s for s in result.project.logic_statements if s.text.startswith("Delay"))
    assert message.reads == ()
    assert delay.reads == ()


def test_v2_packed_st_assignments_and_variable_indexes_fail_closed(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[A := B; C := D;]]></Line>
      <Line Number="1"><![CDATA[Output := Inputs[Index];]]></Line>
    </STContent></Routine>'''
    tags = ''.join(f'<Tag Name="{name}" TagType="Base" DataType="DINT" />' for name in ("A", "B", "C", "D", "Output", "Inputs", "Index"))
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert all(s.semantic_state is PLCSemanticState.PARTIAL for s in result.project.logic_statements)
    indexed = next(s for s in result.project.logic_statements if s.text.startswith("Output"))
    assert "Index" in indexed.reads


def test_v2_nested_aoi_call_is_reference_only_until_local_binding_is_proven(tmp_path: Path) -> None:
    aoi = '''<AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="Inner">
        <Parameters><Parameter Name="In" Usage="Input" DataType="BOOL" Required="true" />
        <Parameter Name="Out" Usage="Output" DataType="BOOL" Required="true" /></Parameters>
        <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(In)OTE(Out);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </AddOnInstructionDefinition>
      <AddOnInstructionDefinition Name="Outer">
        <Parameters><Parameter Name="In" Usage="Input" DataType="BOOL" Required="true" />
        <Parameter Name="Out" Usage="Output" DataType="BOOL" Required="true" /></Parameters>
        <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[Inner(InnerInst,In,Out);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>'''
    body = '<Routine Name="Main" Type="RLL"><RLLContent><Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung></RLLContent></Routine>'
    tags = '<Tag Name="Start" TagType="Base" DataType="BOOL" /><Tag Name="Run" TagType="Base" DataType="BOOL" />'
    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags, aoi=aoi))

    modeled = {item.name: item.internal_body_modeled for item in result.project.aois}
    assert modeled["Inner"] is True
    assert modeled["Outer"] is False
    outer_statement = next(s for s in result.project.logic_statements if s.owner_name == "Outer")
    assert outer_statement.semantic_state is PLCSemanticState.PARTIAL
    assert outer_statement.reads == ()
    assert outer_statement.writes == ()
    assert "Inner" in outer_statement.calls
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v2_refuses_mixed_provenance_if_source_changes_between_passes(tmp_path: Path) -> None:
    from devagent.plc.rockwell_l5x import parse_full_project_l5x
    from devagent.plc.v2_guardrails import verify_v2_source_unchanged

    body = '<Routine Name="Main" Type="RLL"><RLLContent><Rung Number="0"><Text><![CDATA[XIC(A)OTE(O);]]></Text></Rung></RLLContent></Routine>'
    tags = '<Tag Name="A" TagType="Base" DataType="BOOL" /><Tag Name="O" TagType="Base" DataType="BOOL" />'
    path = _write(tmp_path, body, tags=tags)
    project = parse_full_project_l5x(path)
    path.write_text(path.read_text(encoding="utf-8").replace("XIC(A)", "XIO(A)"), encoding="utf-8")

    import pytest
    with pytest.raises(ValueError, match="source changed during analysis"):
        verify_v2_source_unchanged(project)


def test_v2_public_analysis_import_is_guarded() -> None:
    from devagent.plc.analysis import analyze_rockwell_l5x as public_analysis
    from devagent.plc.safe_analysis import analyze_rockwell_l5x as guarded_analysis

    assert public_analysis is guarded_analysis
