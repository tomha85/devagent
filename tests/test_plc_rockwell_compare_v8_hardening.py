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


TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="CompareV8Hardening" TargetType="Controller">
  <Controller Use="Target" Name="CompareV8Hardening" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="{data_type}" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Routines><Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[{rung}]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _project(tmp_path: Path, rung: str, *, data_type: str = "DINT") -> Path:
    path = tmp_path / "CompareV8Hardening.L5X"
    path.write_text(TEMPLATE.format(rung=rung, data_type=data_type), encoding="utf-8")
    return path


def _requirement(text: str) -> PLCRequirement:
    return PLCRequirement(
        "REQ-HARDEN",
        text,
        "requirements.csv",
        "row 2",
        "b" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )


def _verify(path: Path, text: str):
    engineering = analyze_rockwell_l5x(path)
    verification = verify_requirement(
        _requirement(text),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    return engineering, verification


def _compare_check(result):
    return next(item for item in result.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")


def test_v8_requires_ote_to_be_final_instruction(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _project(tmp_path, "GT(Temperature,80)OTE(Fan)XIC(Guard);")
    )
    assert _compare_check(result).status.value == "WARN"
    assert result.outcome.value == "PARTIALLY_VERIFIED"
    assert not any(item.scenario.startswith("THRESHOLD_") for item in result.fat_tests)


def test_v8_rejects_out_of_range_integer_model_threshold(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _project(tmp_path, "GT(Temperature,2147483648)OTE(Fan);")
    )
    assert _compare_check(result).status.value == "WARN"
    assert result.outcome.value == "PARTIALLY_VERIFIED"
    assert not any(item.scenario.startswith("THRESHOLD_") for item in result.fat_tests)


def test_v8_rejects_non_integral_requirement_threshold_for_dint(tmp_path: Path) -> None:
    _, verification = _verify(
        _project(tmp_path, "GT(Temperature,80)OTE(Fan);"),
        "IF Temperature > 80.5 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "not exactly representable" in verification.summary


def test_v8_withholds_vacuous_integer_boundary_requirement(tmp_path: Path) -> None:
    _, verification = _verify(
        _project(tmp_path, "GE(Temperature,2147483647)OTE(Fan);"),
        "IF Temperature > 2147483647 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "no representable DINT witness" in verification.summary


def test_v8_marks_missing_true_or_false_boundary_fat_as_partial(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _project(tmp_path, "GT(Temperature,2147483647)OTE(Fan);")
    )
    assert _compare_check(result).status.value == "WARN"
    assert result.outcome.value == "PARTIALLY_VERIFIED"
    threshold_tests = [item for item in result.fat_tests if item.scenario.startswith("THRESHOLD_")]
    assert {item.scenario for item in threshold_tests} == {"THRESHOLD_FALSE"}


def test_v8_reversed_operand_preserves_compare_direction(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _project(tmp_path, "GT(80,Temperature)OTE(Fan);"),
        "IF Temperature < 80 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert any(item.scenario == "THRESHOLD_TRUE" for item in engineering.fat_tests)
