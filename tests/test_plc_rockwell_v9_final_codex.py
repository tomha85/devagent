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


def _write_project(
    tmp_path: Path,
    *,
    name: str,
    scheduled: bool = True,
    main_routine_name: str | None = "Logic",
    controller_major_fault_program: str | None = None,
    program_name: str = "MainProgram",
    rung_text: str = "XIC(Start)OTE(Fan);",
    extra_tags: str = "",
) -> Path:
    main_attr = f' MainRoutineName="{main_routine_name}"' if main_routine_name is not None else ""
    major_attr = (
        f' MajorFaultProgram="{controller_major_fault_program}"'
        if controller_major_fault_program is not None
        else ""
    )
    scheduled_xml = (
        f'<ScheduledPrograms><ScheduledProgram Name="{program_name}" /></ScheduledPrograms>'
        if scheduled
        else "<ScheduledPrograms />"
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="{name}" TargetType="Controller">
  <Controller Use="Target" Name="{name}" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11"{major_attr}>
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      {extra_tags}
    </Tags>
    <Programs><Program Name="{program_name}"{main_attr}><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS">{scheduled_xml}</Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / f"{name}.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _static_requirement(text: str) -> PLCRequirement:
    return PLCRequirement(
        "REQ-ENTRY",
        text,
        "requirements.json",
        "item 1",
        "f" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )


def test_unscheduled_program_is_partial_and_cannot_create_static_proof_or_fat(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(tmp_path, name="Unscheduled", scheduled=False)
    )
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["unscheduled_executable_programs"] == 1
    assert profile["execution_structure"]["unscheduled_executable_programs"] == ["MainProgram"]
    assert all(item.semantic_state.value == "PARTIAL" for item in engineering.project.output_logic)
    assert not any(test.output_tag == "Fan" for test in engineering.fat_tests)

    verification = verify_requirement(
        _static_requirement("IF Start=TRUE THEN Fan=TRUE"),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED


def test_controller_major_fault_program_is_a_real_entrypoint(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            name="MajorFaultEntry",
            scheduled=False,
            controller_major_fault_program="FaultProgram",
            program_name="FaultProgram",
        )
    )
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.project.metadata.major_fault_program == "FaultProgram"
    assert profile["static_gaps"]["unscheduled_executable_programs"] == 0
    assert profile["execution_structure"]["controller_major_fault_program"] == "FaultProgram"
    assert any(item.semantic_state.value == "FULL" for item in engineering.project.output_logic)
    assert any(test.output_tag == "Fan" for test in engineering.fat_tests)


def test_missing_concrete_main_routine_withholds_program_semantics(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            name="MissingConcreteMain",
            scheduled=True,
            main_routine_name="MissingRoutine",
        )
    )
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["execution_structure_warnings"] >= 1
    assert all(item.semantic_state.value == "PARTIAL" for item in engineering.project.output_logic)
    assert not any(test.output_tag == "Fan" for test in engineering.fat_tests)


def test_limit_compare_rung_is_explicit_support_contract_gap(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            name="LimitGap",
            rung_text="LIMIT(10,Temperature,20)OTE(Fan);",
            extra_tags='<Tag Name="Temperature" TagType="Base" DataType="DINT" />',
        )
    )
    profile = rockwell_capability_profile(engineering.project)
    support = next(check for check in engineering.static_checks if check.id == "ROCKWELL_PRODUCTION_SUPPORT_CONTRACT")

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["unmodeled_compare_rungs"] == 1
    assert profile["typed_compare"]["complex_instruction_names"] == ["LIMIT"]
    assert support.status.value == "WARN"
