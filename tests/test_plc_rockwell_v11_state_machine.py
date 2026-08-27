from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import StaticCheckStatus


def _write(tmp_path: Path, rung_text: str) -> Path:
    tags = ''.join(
        f'<Tag Name="{name}" TagType="Base" DataType="DINT" />'
        for name in ("Step", "Enable")
    )
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="StateMachine" TargetType="Controller">
  <Controller Name="StateMachine" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "state-machine.L5X"
    path.write_text(content, encoding="utf-8")
    return path


def test_v11_discovers_bounded_deterministic_state_transition(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _write(tmp_path, "XIC(Enable)EQU(Step,2)MOV(3,Step);")
    )

    edges = [edge for edge in result.graph.edges if edge.kind == "STATE_TRANSITION"]
    assert len(edges) == 1
    assert edges[0].source == "Step=3"
    assert edges[0].target == "Step=2"

    tests = [item for item in result.fat_tests if item.scenario == "STATE_TRANSITION_RUNTIME"]
    assert len(tests) == 1
    assert tests[0].execution_status == "NOT_RUN"
    assert "2 -> 3" in tests[0].title
    assert tests[0].engineer_execution_required is True
    assert tests[0].setup_steps
    assert tests[0].action_steps
    assert tests[0].evidence_required

    check = next(item for item in result.static_checks if item.id == "ROCKWELL_STATE_MACHINE_DISCOVERY")
    assert check.status is StaticCheckStatus.PASS
    assert "1 bounded deterministic" in check.summary


def test_v11_state_transition_with_motion_is_fat_required_trace_only(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _write(tmp_path, "EQU(Step,2)MCPM(Coord,Path,MotionCtl)MOV(3,Step);")
    )

    tests = [item for item in result.fat_tests if item.scenario == "STATE_TRANSITION_RUNTIME"]
    assert len(tests) == 1
    assert tests[0].execution_status == "NOT_RUN"
    assert "traceable source transition" in tests[0].expected

    check = next(item for item in result.static_checks if item.id == "ROCKWELL_STATE_MACHINE_DISCOVERY")
    assert check.status is StaticCheckStatus.WARN
    assert "1 FAT-required transition" in check.summary
