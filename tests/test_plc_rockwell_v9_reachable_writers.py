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
from devagent.plc.rockwell_alias_hardening import canonical_writer_sources


def _requirement() -> PLCRequirement:
    return PLCRequirement(
        "REQ-COMPARE-WRITER",
        "IF Temperature > 10 THEN Fan=TRUE",
        "requirements.json",
        "item 1",
        "d" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )


def _write(tmp_path: Path, *, schedule_other: bool) -> Path:
    other_schedule = '<ScheduledProgram Name="OtherProgram" />' if schedule_other else ""
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ReachableWriter" TargetType="Controller">
  <Controller Use="Target" Name="ReachableWriter" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Logic"><Routines>
        <Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GRT(Temperature,10)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
      <Program Name="OtherProgram" MainRoutineName="Other"><Routines>
        <Routine Name="Other" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(Guard)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
      {other_schedule}
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / ("BothReachable.L5X" if schedule_other else "OtherUnreachable.L5X")
    path.write_text(payload, encoding="utf-8")
    return path


def test_unreachable_competing_writer_does_not_withhold_reachable_compare_proof(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write(tmp_path, schedule_other=False))

    scenarios = {
        test.scenario
        for test in engineering.fat_tests
        if test.output_tag == "Fan" and test.scenario.startswith("THRESHOLD_")
    }
    assert scenarios == {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}
    verification = verify_requirement(
        _requirement(),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED


def test_reachable_competing_writer_still_withholds_compare_proof(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write(tmp_path, schedule_other=True))

    assert not any(
        test.output_tag == "Fan" and test.scenario.startswith("THRESHOLD_")
        for test in engineering.fat_tests
    )
    verification = verify_requirement(
        _requirement(),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED


def test_two_overlapping_writes_in_one_reachable_rung_are_two_writer_occurrences(tmp_path: Path) -> None:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="SameRungMultiwrite" TargetType="Controller">
  <Controller Use="Target" Name="SameRungMultiwrite" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(FanAlias)XIO(Guard)OTE(Fan);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "SameRungMultiwrite.L5X"
    path.write_text(payload, encoding="utf-8")
    engineering = analyze_rockwell_l5x(path)

    writers = canonical_writer_sources(engineering.project, "FanAlias", "MainProgram")
    assert len(writers) == 2
    assert len(set(writers)) == 1

    requirement = PLCRequirement(
        "REQ-SAME-RUNG",
        "IF Start=TRUE THEN FanAlias=TRUE",
        "requirements.json",
        "item 1",
        "e" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.HIGH,
    )
    verification = verify_requirement(
        requirement,
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED


def test_every_assignment_in_reachable_st_line_counts_as_writer(tmp_path: Path) -> None:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="MultiAssignmentST" TargetType="Controller">
  <Controller Use="Target" Name="MultiAssignmentST" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Temperature" TagType="Base" DataType="DINT" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="Other" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Logic"><Routines>
        <Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GRT(Temperature,10)OTE(Fan);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
      <Program Name="STProgram" MainRoutineName="STLogic"><Routines>
        <Routine Name="STLogic" Type="ST"><STContent>
          <Line Number="0"><![CDATA[Other := FALSE; Fan := FALSE;]]></Line>
        </STContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
      <ScheduledProgram Name="STProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "MultiAssignmentST.L5X"
    path.write_text(payload, encoding="utf-8")
    engineering = analyze_rockwell_l5x(path)

    writers = canonical_writer_sources(engineering.project, "Fan", "MainProgram")
    assert len(writers) == 2
    assert not any(
        test.output_tag == "Fan" and test.scenario.startswith("THRESHOLD_")
        for test in engineering.fat_tests
    )
    verification = verify_requirement(
        _requirement(),
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    assert verification.status is not RequirementStatus.STATICALLY_VERIFIED
