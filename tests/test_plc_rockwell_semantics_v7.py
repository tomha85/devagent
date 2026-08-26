from pathlib import Path

from devagent.plc import analyze_rockwell_l5x


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


def _project(tmp_path: Path) -> Path:
    path = tmp_path / "SemanticV7.L5X"
    path.write_text(PROJECT, encoding="utf-8")
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
