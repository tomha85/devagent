from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_evidence import evidence_index
from devagent.plc.production_models import (
    PLCRequirement,
    RequirementCriticality,
    RequirementStatus,
    RequirementVerificationMode,
)
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_verification import verify_requirement


TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="CompareV8" TargetType="Controller">
  <Controller Use="Target" Name="CompareV8" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes>{data_types}</DataTypes>
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Routines>
          <Routine Name="MainRoutine" Type="RLL"><RLLContent>
            {rungs}
          </RLLContent></Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _project(
    tmp_path: Path,
    *,
    rung: str = "GT(Temperature,80)OTE(Fan);",
    extra_rungs: str = "",
    tags: str | None = None,
    data_types: str = "",
) -> Path:
    tags = tags or (
        '<Tag Name="Temperature" TagType="Base" DataType="REAL" />'
        '<Tag Name="Guard" TagType="Base" DataType="BOOL" />'
        '<Tag Name="Override" TagType="Base" DataType="BOOL" />'
        '<Tag Name="Fan" TagType="Base" DataType="BOOL" />'
    )
    rungs = (
        f'<Rung Number="0"><Text><![CDATA[{rung}]]></Text></Rung>'
        + extra_rungs
    )
    path = tmp_path / "CompareV8.L5X"
    path.write_text(
        TEMPLATE.format(data_types=data_types, tags=tags, rungs=rungs),
        encoding="utf-8",
    )
    return path


def _requirement(text: str) -> PLCRequirement:
    return PLCRequirement(
        "REQ-CMP",
        text,
        "requirements.csv",
        "row 2",
        "a" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )


def _verify(project_path: Path, text: str):
    engineering = analyze_rockwell_l5x(project_path)
    return engineering, verify_requirement(
        _requirement(text),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )


def test_v8_studio5000_v36_gt_generates_typed_threshold_fat(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path))
    assert "GT" not in result.project.unknown_instruction_names
    compare_check = next(
        item for item in result.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS"
    )
    assert compare_check.status.value == "PASS"
    threshold_tests = [
        item for item in result.fat_tests
        if item.scenario in {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}
    ]
    assert len(threshold_tests) == 2
    assert {item.scenario for item in threshold_tests} == {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}
    assert all("Temperature" in item.preconditions for item in threshold_tests)
    assert all(
        isinstance(item.preconditions["Temperature"], (int, float))
        and not isinstance(item.preconditions["Temperature"], bool)
        for item in threshold_tests
    )


def test_v8_old_grt_mnemonic_uses_same_bounded_model(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path, rung="GRT(Temperature,80)OTE(Fan);"))
    assert next(
        item for item in result.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS"
    ).status.value == "PASS"
    assert len([item for item in result.fat_tests if item.scenario.startswith("THRESHOLD_")]) == 2


def test_v8_explicit_threshold_requirement_is_statically_verified_and_linked(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _project(tmp_path),
        "IF Temperature > 80 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert verification.linked_test_ids
    assert any(
        test.id in verification.linked_test_ids and test.scenario == "THRESHOLD_TRUE"
        for test in engineering.fat_tests
    )


def test_v8_stronger_threshold_implies_rung_but_weaker_threshold_does_not(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _, strong = _verify(project, "IF Temperature > 100 THEN Fan=TRUE")
    _, weak = _verify(project, "IF Temperature > 70 THEN Fan=TRUE")
    _, boundary = _verify(project, "IF Temperature >= 80 THEN Fan=TRUE")
    assert strong.status is RequirementStatus.STATICALLY_VERIFIED
    assert weak.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert boundary.status is RequirementStatus.TRACEABLE_NOT_PROVEN


def test_v8_mathematically_incompatible_threshold_is_conflict(tmp_path: Path) -> None:
    _, verification = _verify(
        _project(tmp_path),
        "IF Temperature < 50 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.CONFLICT


def test_v8_extra_boolean_permissive_must_be_explicit_for_true_proof(tmp_path: Path) -> None:
    project = _project(tmp_path, rung="XIC(Guard)GT(Temperature,80)OTE(Fan);")
    _, missing_guard = _verify(project, "IF Temperature > 80 THEN Fan=TRUE")
    _, with_guard = _verify(project, "IF Guard=TRUE AND Temperature > 80 THEN Fan=TRUE")
    assert missing_guard.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert with_guard.status is RequirementStatus.STATICALLY_VERIFIED


def test_v8_multiple_writer_withholds_threshold_fat_and_requirement_proof(tmp_path: Path) -> None:
    second = '<Rung Number="1"><Text><![CDATA[XIC(Override)OTE(Fan);]]></Text></Rung>'
    project = _project(tmp_path, extra_rungs=second)
    engineering, verification = _verify(project, "IF Temperature > 80 THEN Fan=TRUE")
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(item.scenario.startswith("THRESHOLD_") for item in engineering.fat_tests)


def test_v8_udt_numeric_member_type_is_resolved(tmp_path: Path) -> None:
    data_types = (
        '<DataType Name="MOTOR_UDT" Family="NoFamily"><Members>'
        '<Member Name="SpeedRef" DataType="REAL" Dimension="0" Hidden="false" />'
        '</Members></DataType>'
    )
    tags = (
        '<Tag Name="Motor" TagType="Base" DataType="MOTOR_UDT" />'
        '<Tag Name="Fan" TagType="Base" DataType="BOOL" />'
    )
    project = _project(
        tmp_path,
        rung="GT(Motor.SpeedRef,80)OTE(Fan);",
        tags=tags,
        data_types=data_types,
    )
    engineering, verification = _verify(project, "IF Motor.SpeedRef > 80 THEN Fan=TRUE")
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert any("Motor.SpeedRef" in item.preconditions for item in engineering.fat_tests)


def test_v8_branch_compare_and_limit_remain_fail_closed(tmp_path: Path) -> None:
    branch = analyze_rockwell_l5x(
        _project(tmp_path, rung="[GT(Temperature,80),LT(Temperature,20)]OTE(Fan);")
    )
    branch_check = next(
        item for item in branch.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS"
    )
    assert branch_check.status.value == "WARN"
    assert not any(item.scenario.startswith("THRESHOLD_") for item in branch.fat_tests)

    limit_path = tmp_path / "LimitV8.L5X"
    limit_path.write_text(
        TEMPLATE.format(
            data_types="",
            tags=(
                '<Tag Name="Temperature" TagType="Base" DataType="REAL" />'
                '<Tag Name="Fan" TagType="Base" DataType="BOOL" />'
            ),
            rungs='<Rung Number="0"><Text><![CDATA[LIMIT(10,Temperature,20)OTE(Fan);]]></Text></Rung>',
        ),
        encoding="utf-8",
    )
    limit = analyze_rockwell_l5x(limit_path)
    limit_check = next(
        item for item in limit.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS"
    )
    assert limit_check.status.value == "WARN"
    assert limit.outcome.value == "PARTIALLY_VERIFIED"
    assert not any(item.scenario.startswith("THRESHOLD_") for item in limit.fat_tests)


def test_v8_production_pipeline_uses_typed_requirement_theorem(tmp_path: Path) -> None:
    project = _project(tmp_path)
    requirements = tmp_path / "requirements.csv"
    requirements.write_text(
        "id,requirement,verification_mode,criticality\n"
        'REQ-TEMP,"IF Temperature > 80 THEN Fan=TRUE",STATIC,HIGH\n',
        encoding="utf-8",
    )
    result = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
    )
    verification = next(item for item in result.requirement_verification if item.requirement_id == "REQ-TEMP")
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert verification.linked_test_ids
    assert result.stages[6].status.value == "PASS"
