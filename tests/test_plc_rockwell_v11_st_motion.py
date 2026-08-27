from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import PLCSemanticState, StaticCheckStatus


def _write(tmp_path: Path, body: str, *, tags: str) -> Path:
    main_routine = body.split('<Routine Name="', 1)[1].split('"', 1)[0]
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V11" TargetType="Controller">
  <Controller Name="V11" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="{main_routine}"><Routines>{body}</Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V11.L5X"
    path.write_text(content, encoding="utf-8")
    return path


def test_v11_models_bounded_reachable_st_case_dataflow(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[CASE Step OF]]></Line>
      <Line Number="1"><![CDATA[0:]]></Line>
      <Line Number="2"><![CDATA[Command := A;]]></Line>
      <Line Number="3"><![CDATA[1: Command := B;]]></Line>
      <Line Number="4"><![CDATA[ELSE]]></Line>
      <Line Number="5"><![CDATA[Command := C;]]></Line>
      <Line Number="6"><![CDATA[END_CASE;]]></Line>
    </STContent></Routine>'''
    tags = ''.join(
        f'<Tag Name="{name}" TagType="Base" DataType="DINT" />'
        for name in ("Step", "Command", "A", "B", "C")
    )

    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    statements = [item for item in result.project.logic_statements if item.language == "ST"]
    assert len(statements) == 7
    assert all(item.semantic_state is PLCSemanticState.FULL for item in statements)
    assert result.project.st_statement_semantic_count == 7

    command_writes = [item for item in statements if item.writes == ("Command",)]
    assert len(command_writes) == 3
    assert all("Step" in item.reads for item in command_writes)
    assert {"A", "B", "C"} == set().union(*(set(item.reads) - {"Step"} for item in command_writes))

    deps = {(edge.source, edge.target) for edge in result.graph.edges if edge.kind == "DEPENDS_ON"}
    assert {("Command", "Step"), ("Command", "A"), ("Command", "B"), ("Command", "C")} <= deps


def test_v11_case_with_nested_control_remains_partial(tmp_path: Path) -> None:
    body = '''<Routine Name="Sequence" Type="ST"><STContent>
      <Line Number="0"><![CDATA[CASE Step OF]]></Line>
      <Line Number="1"><![CDATA[0:]]></Line>
      <Line Number="2"><![CDATA[IF Enable THEN]]></Line>
      <Line Number="3"><![CDATA[Command := A;]]></Line>
      <Line Number="4"><![CDATA[END_IF;]]></Line>
      <Line Number="5"><![CDATA[END_CASE;]]></Line>
    </STContent></Routine>'''
    tags = ''.join(
        f'<Tag Name="{name}" TagType="Base" DataType="DINT" />'
        for name in ("Step", "Enable", "Command", "A")
    )

    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    assert any(
        item.semantic_state is PLCSemanticState.PARTIAL
        for item in result.project.logic_statements
        if item.language == "ST"
    )


def test_v11_motion_generates_engineer_fat_without_static_pass(tmp_path: Path) -> None:
    body = '''<Routine Name="Motion" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[XIC(Enable)MAJ(Axis1,JogControl,1,100.0);]]></Text></Rung>
      <Rung Number="1"><Text><![CDATA[MCPM(CoordSys,PathData,MotionControl);]]></Text></Rung>
    </RLLContent></Routine>'''
    tags = '''
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Axis1" TagType="Base" DataType="AXIS_CIP_DRIVE" />
      <Tag Name="JogControl" TagType="Base" DataType="MOTION_INSTRUCTION" />
      <Tag Name="CoordSys" TagType="Base" DataType="COORDINATE_SYSTEM" />
      <Tag Name="PathData" TagType="Base" DataType="DINT" />
      <Tag Name="MotionControl" TagType="Base" DataType="MOTION_INSTRUCTION" />'''

    result = analyze_rockwell_l5x(_write(tmp_path, body, tags=tags))

    tests = [item for item in result.fat_tests if item.scenario == "MOTION_RUNTIME"]
    assert len(tests) == 2
    assert all(item.execution_status == "NOT_RUN" for item in tests)
    assert all("does not connect" in " ".join(item.limitations).lower() for item in tests)
    assert all(item.engineer_execution_required for item in tests)
    assert all(item.purpose and item.setup_steps and item.action_steps for item in tests)
    assert all(item.evidence_required and item.failure_implication for item in tests)
    assert {"Axis1", "CoordSys"} == {item.output_tag for item in tests}

    check = next(item for item in result.static_checks if item.id == "ROCKWELL_MOTION_RUNTIME_CONTRACT")
    assert check.status is StaticCheckStatus.WARN
    assert "remains PARTIAL" in check.summary
    assert set(result.project.partially_modeled_instruction_names) >= {"MAJ", "MCPM"}
