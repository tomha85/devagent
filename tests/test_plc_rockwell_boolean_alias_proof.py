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
        "REQ-BOOL-ALIAS",
        text,
        "requirements.csv",
        "row 2",
        "f" * 64,
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


def _write(tmp_path: Path, *, second_writer: str = "FanAlias") -> Path:
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="BoolAlias" TargetType="Controller">
  <Controller Use="Target" Name="BoolAlias" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Override" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Fan);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(Override)OTE({second_writer});]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "BoolAlias.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_boolean_requirement_alias_writer_blocks_static_verification(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write(tmp_path),
        "IF Start=TRUE THEN Fan=TRUE",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "canonical executable writer" in verification.summary
    assert len(verification.evidence_ids) >= 2
    assert engineering.project.tags[-1].alias_for == "Fan"


def test_boolean_requirement_alias_writer_blocks_false_conflict_claim(tmp_path: Path) -> None:
    _, verification = _verify(
        _write(tmp_path),
        "IF Start=TRUE THEN Fan=FALSE",
    )
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "canonical executable writer" in verification.summary


def test_boolean_requirement_single_physical_writer_still_verifies(tmp_path: Path) -> None:
    path = _write(tmp_path, second_writer="Unrelated")
    # Add the unrelated tag without introducing another writer for Fan.
    text = path.read_text(encoding="utf-8").replace(
        '<Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" />',
        '<Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" /><Tag Name="Unrelated" TagType="Base" DataType="BOOL" />',
    )
    path.write_text(text, encoding="utf-8")
    _, verification = _verify(path, "IF Start=TRUE THEN Fan=TRUE")
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
