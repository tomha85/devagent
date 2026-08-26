from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.rockwell_stateful_runtime import stateful_models, stateful_profile


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Stateful" TargetType="Controller">
  <Controller Name="Stateful" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Pulse" TagType="Base" DataType="BOOL" />
      <Tag Name="Timer1" TagType="Base" DataType="TIMER" />
      <Tag Name="Counter1" TagType="Base" DataType="COUNTER" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Enable)TON(Timer1,1000,0);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(Pulse)CTU(Counter1,10,0);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "Stateful.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_stateful_instructions_are_partial_but_generate_runtime_fat(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_project(tmp_path))

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert {"TON", "CTU"} <= set(result.project.partially_modeled_instruction_names)
    models = stateful_models(result.project)
    assert {model.instruction for model in models} == {"TON", "CTU"}

    tests = [item for item in result.fat_tests if item.id.startswith("FAT-STATEFUL-")]
    assert len(tests) == 2
    assert {item.output_tag for item in tests} == {"Timer1", "Counter1"}
    assert all(item.execution_status == "NOT_RUN" for item in tests)
    assert all(item.scenario == "STATEFUL_RUNTIME" for item in tests)
    assert any("false-to-true" in item.expected for item in tests if item.output_tag == "Counter1")
    assert any("controller time" in item.expected for item in tests if item.output_tag == "Timer1")

    check = next(
        item for item in result.static_checks
        if item.id == "ROCKWELL_STATEFUL_RUNTIME_SEMANTICS"
    )
    assert check.status is StaticCheckStatus.WARN
    assert any("qualified runtime evidence" in item for item in result.limitations)


def test_stateful_profile_reports_runtime_evidence_requirement(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_project(tmp_path))
    profile = stateful_profile(result.project)

    assert profile["schema"] == "devagent-rockwell-stateful-runtime-v1"
    assert profile["modeled_occurrences"] == 2
    assert profile["instructions"] == {"CTU": 1, "TON": 1}
    assert profile["requires_qualified_runtime_evidence"] is True
