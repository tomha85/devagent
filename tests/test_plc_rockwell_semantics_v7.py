from dataclasses import replace
from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import PLCDependencyGraph, PLCInstruction, PLCOutcome
from devagent.plc.rockwell_structure import add_rockwell_structure_edges


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="SemanticV7" TargetType="Controller">
  <Controller Use="Target" Name="SemanticV7" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes>
      <DataType Name="MOTOR_UDT" Family="NoFamily">
        <Members>
          <Member Name="RunCmd" DataType="BOOL" Dimension="0" Hidden="false"><Description>Run command</Description></Member>
          <Member Name="SpeedRef" DataType="REAL" Dimension="0" Hidden="false" />
        </Members>
      </DataType>
    </DataTypes>
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine" FaultRoutineName="FaultRoutine">
        <Routines>
          <Routine Name="MainRoutine" Type="RLL"><RLLContent>
            <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
            <Rung Number="1"><Text><![CDATA[JSR(Helper);]]></Text></Rung>
          </RLLContent></Routine>
          <Routine Name="Helper" Type="RLL"><RLLContent>
            <Rung Number="0"><Text><![CDATA[NOP();]]></Text></Rung>
          </RLLContent></Routine>
          <Routine Name="FaultRoutine" Type="RLL"><RLLContent>
            <Rung Number="0"><Text><![CDATA[NOP();]]></Text></Rung>
          </RLLContent></Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks>
      <Task Name="MainTask" Type="CONTINUOUS" Priority="10">
        <ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms>
      </Task>
    </Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _project(tmp_path: Path, content: str = PROJECT) -> Path:
    path = tmp_path / "SemanticV7.L5X"
    path.write_text(content, encoding="utf-8")
    return path


def test_v7_normalizes_task_program_routine_and_udt_structure(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path))
    project = result.project

    task = next(item for item in project.tasks if item.name == "MainTask")
    assert task.scheduled_programs == ("MainProgram",)

    program = next(item for item in project.programs if item.name == "MainProgram")
    assert program.main_routine_name == "MainRoutine"
    assert program.fault_routine_name == "FaultRoutine"

    udt = next(item for item in project.data_types if item.name == "MOTOR_UDT")
    assert [(item.name, item.data_type) for item in udt.members] == [
        ("RunCmd", "BOOL"),
        ("SpeedRef", "REAL"),
    ]
    assert udt.members[0].description == "Run command"


def test_v7_emits_typed_execution_structure_edges(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path))
    graph = result.graph
    edges = {(edge.source, edge.target, edge.kind) for edge in graph.edges}

    task = next(item for item in result.project.tasks if item.name == "MainTask")
    program = next(item for item in result.project.programs if item.name == "MainProgram")
    main = next(item for item in result.project.routines if item.program == "MainProgram" and item.name == "MainRoutine")
    helper = next(item for item in result.project.routines if item.program == "MainProgram" and item.name == "Helper")
    fault = next(item for item in result.project.routines if item.program == "MainProgram" and item.name == "FaultRoutine")
    jsr_rung = next(item for item in result.project.rungs if item.program == "MainProgram" and item.number == "1")

    assert (task.id, program.id, "SCHEDULES") in edges
    assert (program.id, main.id, "ENTRYPOINT") in edges
    assert (program.id, fault.id, "FAULT_ROUTINE") in edges
    assert (jsr_rung.id, helper.id, "CALLS_ROUTINE") in edges


def test_v7_structure_static_check_is_auditable(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path))
    check = next(item for item in result.static_checks if item.id == "ROCKWELL_EXECUTION_STRUCTURE")
    assert check.status.value == "PASS"
    assert "1 task→program schedule entries" in check.summary
    assert "1 main routine assignment" in check.summary
    assert "1 fault routine assignment" in check.summary
    assert "2 UDT member definition" in check.summary


def test_v7_missing_scheduled_program_downgrades_outcome(tmp_path: Path) -> None:
    broken = PROJECT.replace(
        '<ScheduledProgram Name="MainProgram" />',
        '<ScheduledProgram Name="MissingProgram" />',
    )
    result = analyze_rockwell_l5x(_project(tmp_path, broken))
    check = next(item for item in result.static_checks if item.id == "ROCKWELL_EXECUTION_STRUCTURE")
    assert check.status.value == "WARN"
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any("schedules missing program" in item for item in check.evidence)


def test_v7_unresolved_jsr_downgrades_outcome_and_withholds_edge(tmp_path: Path) -> None:
    broken = PROJECT.replace("JSR(Helper);", "JSR(MissingRoutine);")
    result = analyze_rockwell_l5x(_project(tmp_path, broken))
    check = next(item for item in result.static_checks if item.id == "ROCKWELL_EXECUTION_STRUCTURE")
    assert check.status.value == "WARN"
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any("calls missing routine MissingRoutine" in item for item in check.evidence)
    assert not any(edge.kind == "CALLS_ROUTINE" for edge in result.graph.edges if "rung/1" in edge.source)


def test_v7_aoi_named_like_routine_does_not_create_false_routine_edge(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path))
    original = next(item for item in result.project.rungs if item.program == "MainProgram" and item.number == "1")
    fake_aoi_call = replace(
        original,
        instructions=(PLCInstruction(name="Helper", arguments=("Backing",)),),
        calls=("Helper",),
    )
    result.project.rungs = [fake_aoi_call if item.id == original.id else item for item in result.project.rungs]
    graph = PLCDependencyGraph()
    add_rockwell_structure_edges(result.project, graph)
    helper = next(item for item in result.project.routines if item.program == "MainProgram" and item.name == "Helper")
    assert (fake_aoi_call.id, helper.id, "CALLS_ROUTINE") not in {
        (edge.source, edge.target, edge.kind) for edge in graph.edges
    }
