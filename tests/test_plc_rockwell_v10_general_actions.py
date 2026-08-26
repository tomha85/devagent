from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import StaticCheckStatus
from devagent.plc.rockwell_general_actions import action_models, action_profile


def _write_project(tmp_path: Path, *, include_compare_gate: bool = False) -> Path:
    compare_rung = (
        '<Rung Number="4"><Text><![CDATA[EQU(Source,5)MOV(Source,CompareDest);]]></Text></Rung>'
        if include_compare_gate
        else ''
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="GeneralActions" TargetType="Controller">
  <Controller Name="GeneralActions" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Reset" TagType="Base" DataType="BOOL" />
      <Tag Name="A" TagType="Base" DataType="BOOL" />
      <Tag Name="B" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="X" TagType="Base" DataType="DINT" />
      <Tag Name="Y" TagType="Base" DataType="DINT" />
      <Tag Name="Sum" TagType="Base" DataType="DINT" />
      <Tag Name="Count" TagType="Base" DataType="DINT" />
      <Tag Name="Timer1" TagType="Base" DataType="TIMER" />
      <Tag Name="CompareDest" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Enable)MOV(Source,Dest);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[[XIC(A),XIC(B)]ADD(X,Y,Sum);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[XIC(Reset)CLR(Count);]]></Text></Rung>
        <Rung Number="3"><Text><![CDATA[XIC(Enable)TON(Timer1,1000,0);]]></Text></Rung>
        {compare_rung}
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "GeneralActions.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_action_paths_generate_models_dependencies_and_fat(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    models = action_models(engineering.project)

    assert {item.instruction for item in models} == {"MOV", "ADD", "CLR"}
    assert {item.output_tag for item in models} == {"Dest", "Sum", "Count"}

    mov = next(item for item in models if item.instruction == "MOV")
    assert mov.input_refs == ("Source",)
    assert len(mov.paths) == 1
    assert [(term.tag, term.required) for term in mov.paths[0].terms] == [("Enable", True)]

    add = next(item for item in models if item.instruction == "ADD")
    assert len(add.paths) == 2
    assert {tuple((term.tag, term.required) for term in path.terms) for path in add.paths} == {
        (("A", True),),
        (("B", True),),
    }

    dependency_edges = {
        (edge.source, edge.target, edge.evidence_id)
        for edge in engineering.graph.edges
        if edge.kind == "DEPENDS_ON"
    }
    assert ("Dest", "Source", mov.id) in dependency_edges
    assert ("Dest", "Enable", mov.id) in dependency_edges

    action_tests = [item for item in engineering.fat_tests if item.id.startswith("FAT-ACTION-")]
    assert {item.output_tag for item in action_tests} == {"Dest", "Sum", "Count"}
    assert all(item.execution_status == "NOT_RUN" for item in action_tests)
    assert all(item.scenario == "ACTION_PATH" for item in action_tests)
    assert not any(item.output_tag == "Timer1" for item in action_tests)

    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_ACTION_PATH_SEMANTICS")
    assert check.status is StaticCheckStatus.PASS


def test_compare_gated_action_fails_closed_until_cross_family_path_is_modeled(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path, include_compare_gate=True))

    assert not any(
        item.output_tag == "CompareDest"
        for item in engineering.fat_tests
        if item.id.startswith("FAT-ACTION-")
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_ACTION_PATH_SEMANTICS")
    assert check.status is StaticCheckStatus.WARN
    assert check.evidence
    assert any("action-path theorem" in item for item in engineering.limitations)


def test_action_profile_is_generic_and_project_bound(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    profile = action_profile(engineering.project)

    assert profile["schema"] == "devagent-rockwell-action-semantics-v1"
    assert profile["modeled_actions"] == 3
    assert profile["withheld_rungs"] == 0
    assert profile["instructions"] == {"ADD": 1, "CLR": 1, "MOV": 1}
    assert profile["families"]["COMPUTE"] == 1
    assert profile["families"]["DATA_MOVE"] == 1
