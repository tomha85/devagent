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
        "REQ-IDENTITY",
        text,
        "requirements.csv",
        "row 2",
        "d" * 64,
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


def _write_casefold_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8Identity" TargetType="Controller">
  <Controller Use="Target" Name="V8Identity" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Override" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
        <Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GT(Temperature,80)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
      <Program Name="OverrideProgram" MainRoutineName="OverrideRoutine"><Routines>
        <Routine Name="OverrideRoutine" Type="ST"><STContent>
          <Line Number="0"><![CDATA[fan := Override;]]></Line>
        </STContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" /><ScheduledProgram Name="OverrideProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8IdentityCasefold.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_program_qualified_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8ProgramIdentity" TargetType="Controller">
  <Controller Use="Target" Name="V8ProgramIdentity" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Override" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Tags><Tag Name="Fan" TagType="Base" DataType="BOOL" /></Tags>
        <Routines><Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GT(Temperature,80)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </Program>
      <Program Name="OverrideProgram" MainRoutineName="OverrideRoutine"><Routines>
        <Routine Name="OverrideRoutine" Type="ST"><STContent>
          <Line Number="0"><![CDATA[Program:MainProgram.Fan := Override;]]></Line>
        </STContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" /><ScheduledProgram Name="OverrideProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8ProgramIdentity.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_independent_program_fans(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8IndependentScopes" TargetType="Controller">
  <Controller Use="Target" Name="V8IndependentScopes" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="OtherStart" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine">
        <Tags><Tag Name="Fan" TagType="Base" DataType="BOOL" /></Tags>
        <Routines><Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GT(Temperature,80)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </Program>
      <Program Name="OtherProgram" MainRoutineName="OtherRoutine">
        <Tags><Tag Name="Fan" TagType="Base" DataType="BOOL" /></Tags>
        <Routines><Routine Name="OtherRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(OtherStart)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" /><ScheduledProgram Name="OtherProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8IndependentScopes.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v8_casefolded_st_writer_blocks_threshold_proof(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write_casefold_project(tmp_path),
        "IF Temperature > 80 THEN Fan=TRUE",
    )
    assert any(statement.language == "ST" and "fan" in statement.writes for statement in engineering.project.logic_statements)
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_program_qualified_writer_blocks_program_scope_output(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write_program_qualified_project(tmp_path),
        "IF Temperature > 80 THEN Fan=TRUE",
    )
    assert any(
        statement.language == "ST" and "Program:MainProgram.Fan" in statement.writes
        for statement in engineering.project.logic_statements
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_contradictory_output_consequent_is_not_proven(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_casefold_project(tmp_path))
    verification = verify_requirement(
        _requirement("IF Temperature > 80 THEN Fan=TRUE AND Fan=FALSE"),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "exactly one unambiguous output-state assertion" in verification.summary


def test_v8_independent_program_scoped_fans_remain_independent_writers(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_independent_program_fans(tmp_path))
    threshold_tests = [
        test for test in engineering.fat_tests
        if test.scenario.startswith("THRESHOLD_") and test.source.program == "MainProgram"
    ]
    assert {test.scenario for test in threshold_tests} == {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert "additional executable writers" not in check.summary
