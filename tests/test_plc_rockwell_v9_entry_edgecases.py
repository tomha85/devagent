from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_evidence import evidence_index
from devagent.plc.production_models import (
    PLCRequirement,
    RequirementCriticality,
    RequirementStatus,
    RequirementVerificationMode,
)
from devagent.plc.production_verification import verify_requirement
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _requirement(text: str) -> PLCRequirement:
    return PLCRequirement(
        "REQ-ENTRY-EDGE",
        text,
        "requirements.json",
        "item 1",
        "e" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )


def test_inhibited_task_is_not_an_executable_compare_entry(tmp_path: Path) -> None:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="InhibitedEntry" TargetType="Controller">
  <Controller Use="Target" Name="InhibitedEntry" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[GRT(Temperature,10)OTE(Fan);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="InhibitedTask" Type="PERIODIC" InhibitTask="true"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "InhibitedEntry.L5X"
    path.write_text(payload, encoding="utf-8")

    engineering = analyze_rockwell_l5x(path)
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["execution_structure"]["inhibited_tasks"] == ["InhibitedTask"]
    assert profile["static_gaps"]["unscheduled_executable_programs"] == 1
    assert not any(
        test.output_tag == "Fan" and test.scenario.startswith("THRESHOLD_")
        for test in engineering.fat_tests
    )

    verification = verify_requirement(
        _requirement("IF Temperature > 10 THEN Fan=TRUE"),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED


def test_empty_active_entry_program_requires_concrete_main(tmp_path: Path) -> None:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="EmptyEntry" TargetType="Controller">
  <Controller Use="Target" Name="EmptyEntry" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="Live" MainRoutineName="Logic"><Routines>
        <Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
      <Program Name="Empty"><Routines /></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="Live" />
      <ScheduledProgram Name="Empty" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "EmptyEntry.L5X"
    path.write_text(payload, encoding="utf-8")

    engineering = analyze_rockwell_l5x(path)
    profile = rockwell_capability_profile(engineering.project)

    assert any(test.output_tag == "Fan" for test in engineering.fat_tests)
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["scheduled_programs_without_main_routine"] == 1
    assert profile["execution_structure"]["scheduled_programs_without_main_routine"] == ["Empty"]
