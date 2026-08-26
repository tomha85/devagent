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
        "REQ-ALIAS",
        text,
        "requirements.csv",
        "row 2",
        "e" * 64,
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


def _write_alias_project(tmp_path: Path, *, compare_output: str = "Fan", other_output: str = "FanAlias") -> Path:
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8Alias" TargetType="Controller">
  <Controller Use="Target" Name="V8Alias" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Override" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" />
      <Tag Name="FanAlias2" TagType="Alias" DataType="BOOL" AliasFor="FanAlias" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
        <Routine Name="MainRoutine" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GT(Temperature,80)OTE({compare_output});]]></Text></Rung>
          <Rung Number="1"><Text><![CDATA[XIC(Override)OTE({other_output});]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8Alias.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_member_alias_project(tmp_path: Path, *, dangling: bool = False) -> Path:
    alias_for = "MissingStatus.0" if dangling else "Status.0"
    whole_writer = "" if dangling else '<Rung Number="1"><Text><![CDATA[MOV(0,Status);]]></Text></Rung>'
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V8AliasMember" TargetType="Controller">
  <Controller Use="Target" Name="V8AliasMember" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Status" TagType="Base" DataType="DINT" />
      <Tag Name="RunAlias" TagType="Alias" DataType="BOOL" AliasFor="{alias_for}" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[GT(Temperature,80)OTE(RunAlias);]]></Text></Rung>
        {whole_writer}
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "V8AliasMember.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v8_alias_writer_blocks_base_tag_threshold_proof(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write_alias_project(tmp_path),
        "IF Temperature > 80 THEN Fan=TRUE",
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_base_writer_blocks_alias_output_threshold_proof(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_alias_project(tmp_path, compare_output="FanAlias", other_output="Fan")
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_alias_chain_resolves_to_same_underlying_writer(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_alias_project(tmp_path, compare_output="Fan", other_output="FanAlias2")
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert "additional executable writers" in check.summary
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_whole_tag_write_overlaps_alias_member_storage(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write_member_alias_project(tmp_path),
        "IF Temperature > 80 THEN RunAlias=TRUE",
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)


def test_v8_dangling_alias_target_is_withheld_from_compare_proof(tmp_path: Path) -> None:
    engineering, verification = _verify(
        _write_member_alias_project(tmp_path, dangling=True),
        "IF Temperature > 80 THEN RunAlias=TRUE",
    )
    check = next(item for item in engineering.static_checks if item.id == "ROCKWELL_TYPED_COMPARE_SEMANTICS")
    assert check.status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED
    assert not any(test.scenario.startswith("THRESHOLD_") for test in engineering.fat_tests)
