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


def _requirement(text: str) -> PLCRequirement:
    return PLCRequirement(
        "REQ-REVIEW",
        text,
        "requirements.csv",
        "row 2",
        "c" * 64,
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


def _write(tmp_path: Path, *, rung: str, data_type: str = "DINT", extra_program: str = "", schedules: str = '<ScheduledProgram Name="MainProgram" />') -> Path:
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8Review" TargetType="Controller">
  <Controller Use="Target" Name="V8Review" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="{data_type}" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Override" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
        <Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[{rung}]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
      {extra_program}
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>{schedules}</ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8Review.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v8_st_writer_prevents_single_writer_threshold_proof(tmp_path: Path) -> None:
    st_program = '''<Program Name="OverrideProgram" MainRoutineName="OverrideRoutine"><Routines>
      <Routine Name="OverrideRoutine" Type="ST"><STContent>
        <Line Number="0"><![CDATA[Fan := Override;]]></Line>
      </STContent></Routine>
    </Routines></Program>'''
    path = _write(
        tmp_path,
        rung="GT(Temperature,80)OTE(Fan);",
        extra_program=st_program,
        schedules='<ScheduledProgram Name="MainProgram" /><ScheduledProgram Name="OverrideProgram" />',
    )
    engineering, verification = _verify(path, "IF Temperature > 80 THEN Fan=TRUE")
    assert any(statement.language == "ST" and "Fan" in statement.writes for statement in engineering.project.logic_statements)
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(item.scenario.startswith("THRESHOLD_") for item in engineering.fat_tests)


def test_v8_fractional_dint_model_threshold_is_withheld(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _write(tmp_path, rung="GE(Temperature,80.5)OTE(Fan);")
    )
    check = next(item for item in result.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert result.outcome.value == "PARTIALLY_VERIFIED"
    assert not any(item.scenario.startswith("THRESHOLD_") for item in result.fat_tests)


def test_v8_or_antecedent_is_not_statically_proven(tmp_path: Path) -> None:
    path = _write(tmp_path, rung="XIC(Guard)GT(Temperature,80)OTE(Fan);")
    _, verification = _verify(
        path,
        "IF Guard=TRUE OR Temperature > 80 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "OR/XOR" in verification.summary


def test_v8_reversed_requirement_implication_is_not_statically_proven(tmp_path: Path) -> None:
    path = _write(tmp_path, rung="GT(Temperature,80)OTE(Fan);")
    _, verification = _verify(
        path,
        "IF Fan=TRUE THEN Temperature > 80",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "implication direction" in verification.summary


def test_v8_conjunctive_antecedent_still_verifies(tmp_path: Path) -> None:
    path = _write(tmp_path, rung="XIC(Guard)GT(Temperature,80)OTE(Fan);")
    _, verification = _verify(
        path,
        "IF Guard=TRUE AND Temperature > 80 THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
