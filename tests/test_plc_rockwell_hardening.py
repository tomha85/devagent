from __future__ import annotations

from pathlib import Path

from devagent.plc.analysis import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, StaticCheckStatus


def _project(tmp_path: Path, rung_text: str, *, aoi_xml: str = "<AddOnInstructionDefinitions />") -> Path:
    content = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="HardeningController" TargetType="Controller" ContainsContext="false">
  <Controller Use="Target" Name="HardeningController" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    {aoi_xml}
    <Tags>
      <Tag Name="Inputs" TagType="Base" DataType="DINT" />
      <Tag Name="Output" TagType="Base" DataType="BOOL" />
      <Tag Name="Storage" TagType="Base" DataType="BOOL" />
      <Tag Name="Pulse" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="ScanTime" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs>
      <Program Name="MainProgram">
        <Routines>
          <Routine Name="MainRoutine" Type="RLL">
            <RLLContent>
              <Rung Number="0" Type="N"><Text><![CDATA[{rung_text}]]></Text></Rung>
            </RLLContent>
          </Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" /></Tasks>
  </Controller>
</RSLogix5000Content>
'''
    path = tmp_path / "Hardening.L5X"
    path.write_text(content, encoding="utf-8")
    return path


def test_array_subscript_is_not_mistaken_for_parallel_branch(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path, "XIC(Inputs[0])OTE(Output);"))

    branch = next(check for check in result.static_checks if check.id == "BRANCH_DEPENDENCY_SEMANTICS")
    assert branch.status is StaticCheckStatus.PASS
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert any(
        edge.kind == "DEPENDS_ON" and edge.source == "Output" and edge.target == "Inputs[0]"
        for edge in result.graph.edges
    )
    assert len(result.fat_tests) == 1


def test_instruction_free_rung_cannot_be_statically_verified(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_project(tmp_path, ";"))

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    coverage = next(check for check in result.static_checks if check.id == "LOGIC_SEMANTIC_COVERAGE")
    assert coverage.status is StaticCheckStatus.NOT_PROVEN
    assert any("No executable RLL instructions" in item for item in result.limitations)


def test_unprotected_aoi_body_remains_not_proven(tmp_path: Path) -> None:
    aoi = '''<AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="CustomAOI">
        <Parameters>
          <Parameter Name="Enable" Usage="Input" DataType="BOOL" />
          <Parameter Name="Command" Usage="Output" DataType="BOOL" />
        </Parameters>
        <Routines>
          <Routine Name="Logic" Type="RLL">
            <RLLContent><Rung Number="0" Type="N"><Text><![CDATA[XIC(Enable)OTE(Command);]]></Text></Rung></RLLContent>
          </Routine>
        </Routines>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>'''
    result = analyze_rockwell_l5x(_project(tmp_path, "CustomAOI(Output,Pulse);", aoi_xml=aoi))

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    aoi_check = next(check for check in result.static_checks if check.id == "AOI_INTERNAL_LOGIC")
    assert aoi_check.status is StaticCheckStatus.NOT_PROVEN
    assert any("internal routines are not normalized" in item for item in result.limitations)


def test_osr_and_osf_record_distinct_output_operands(tmp_path: Path) -> None:
    rising = analyze_rockwell_l5x(_project(tmp_path, "OSR(Storage,Pulse);"))
    rung = rising.project.rungs[0]
    assert "Storage" in rung.reads
    assert "Storage" in rung.writes
    assert "Pulse" in rung.writes

    falling_path = _project(tmp_path, "OSF(Storage,Pulse);")
    falling_path.rename(tmp_path / "Falling.L5X")
    falling = analyze_rockwell_l5x(tmp_path / "Falling.L5X")
    rung = falling.project.rungs[0]
    assert "Storage" in rung.reads
    assert "Storage" in rung.writes
    assert "Pulse" in rung.writes


def test_gsv_ssv_and_v36_move_are_normalized_from_vendor_semantics(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _project(
            tmp_path,
            "GSV(Program,THIS,LastScanTime,ScanTime)MOVE(ScanTime,Dest)SSV(Program,THIS,LastScanTime,Dest);",
        )
    )
    rung = result.project.rungs[0]

    assert result.project.instruction_semantic_coverage == 1.0
    assert result.project.unknown_instruction_names == []
    assert "ScanTime" in rung.writes
    assert "ScanTime" in rung.reads
    assert "Dest" in rung.writes
    assert "Dest" in rung.reads
    assert not any(edge.kind == "DEPENDS_ON" for edge in result.graph.edges)


def test_true_parallel_branch_still_withholds_cross_branch_dependencies(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(
        _project(tmp_path, "[XIC(Inputs[0])OTE(Output),XIC(Inputs[1])OTE(Pulse)];")
    )

    branch = next(check for check in result.static_checks if check.id == "BRANCH_DEPENDENCY_SEMANTICS")
    assert branch.status is StaticCheckStatus.WARN
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert not any(edge.kind == "DEPENDS_ON" for edge in result.graph.edges)
    assert result.fat_tests == []
