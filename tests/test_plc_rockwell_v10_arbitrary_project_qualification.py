from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest
from devagent.plc.rockwell_standard_catalog import standard_catalog_profile


def _write_mixed_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="MixedQualification" TargetType="Controller">
  <Controller Name="MixedQualification" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="PassThrough">
        <Parameters>
          <Parameter Name="In" Usage="Input" DataType="BOOL" Required="true" />
          <Parameter Name="Out" Usage="Output" DataType="BOOL" Required="true" />
        </Parameters>
        <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(In)OTE(Out);]]></Text></Rung>
        </RLLContent></Routine></Routines>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="X" TagType="Base" DataType="DINT" />
      <Tag Name="Y" TagType="Base" DataType="DINT" />
      <Tag Name="Sum" TagType="Base" DataType="DINT" />
      <Tag Name="Timer1" TagType="Base" DataType="TIMER" />
      <Tag Name="Axis1" TagType="Base" DataType="AXIS_CIP_DRIVE" />
      <Tag Name="JogControl" TagType="Base" DataType="MOTION_INSTRUCTION" />
      <Tag Name="STOut" TagType="Base" DataType="REAL" />
      <Tag Name="MysteryIn" TagType="Base" DataType="DINT" />
      <Tag Name="MysteryOut" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Enable)OTE(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(Enable)MOV(Source,Dest);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[XIC(Enable)ADD(X,Y,Sum);]]></Text></Rung>
        <Rung Number="3"><Text><![CDATA[XIC(Enable)TON(Timer1,1000,0);]]></Text></Rung>
        <Rung Number="4"><Text><![CDATA[JSR(Sequence,0);]]></Text></Rung>
        <Rung Number="5"><Text><![CDATA[MAJ(Axis1,JogControl,1,100.0);]]></Text></Rung>
        <Rung Number="6"><Text><![CDATA[VendorMystery(MysteryIn,MysteryOut);]]></Text></Rung>
      </RLLContent></Routine>
      <Routine Name="Sequence" Type="ST"><STContent>
        <Line Number="0"><![CDATA[STOut := REAL(Source);]]></Line>
      </STContent></Routine>
      <Routine Name="Diagram" Type="FBD" />
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "MixedQualification.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_catalog_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Catalog" TargetType="Controller">
  <Controller Name="Catalog" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Storage" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="MsgCtl" TagType="Base" DataType="MESSAGE" />
      <Tag Name="A" TagType="Base" DataType="DINT" />
      <Tag Name="B" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)ONS(Storage)OTE(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[MSG(MsgCtl);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[VendorPrivate(A,B);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "Catalog.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_mixed_arbitrary_project_loads_tests_supported_surfaces_and_fails_closed(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_mixed_project(tmp_path))
    manifest = build_semantic_coverage_manifest(result.project)

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert result.project.metadata.full_project is True
    assert result.project.metadata.controller_name == "MixedQualification"
    assert len(result.project.aois) == 1
    assert result.project.aoi_internal_modeled_count == 1

    # Supported reachable logic is still understood even though other project
    # regions are partial/unsupported.
    assert any(item.output_tag == "Run" for item in result.project.output_logic)
    assert manifest["action_semantics"]["modeled_actions"] >= 2
    assert manifest["stateful_runtime_semantics"]["modeled_occurrences"] == 1
    assert manifest["language_summary"]["structured_text"]["reachable_full_dataflow_statements"] == 1

    # Unsupported/opaque surfaces remain explicit rather than poisoning import or
    # being silently treated as verified.
    assert manifest["project_boundaries"]["unsupported_routine_types"] == {"FBD": 1}
    assert "MAJ" in manifest["project_boundaries"]["partially_modeled_instruction_names"]
    assert "TON" in manifest["project_boundaries"]["partially_modeled_instruction_names"]
    assert "VENDORMYSTERY" in manifest["project_boundaries"]["unmodeled_instruction_names"]

    scenarios = {item.scenario for item in result.fat_tests}
    assert "POSITIVE_PATH" in scenarios
    assert "ACTION_PATH" in scenarios
    assert "STATEFUL_RUNTIME" in scenarios
    assert all(item.execution_status == "NOT_RUN" for item in result.fat_tests)
    assert not any(item.output_tag in {"Axis1", "MysteryOut"} for item in result.fat_tests)


def test_standard_catalog_separates_known_partial_from_true_unknown(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_catalog_project(tmp_path))
    profile = standard_catalog_profile(result.project)

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert {"ONS", "MSG"} <= set(result.project.partially_modeled_instruction_names)
    assert "VENDORPRIVATE" in {name.upper() for name in result.project.unknown_instruction_names}
    assert profile["families"]["EDGE_STATE"]["instructions"] == ["ONS"]
    assert profile["families"]["COMMUNICATION"]["instructions"] == ["MSG"]
    check = next(
        item for item in result.static_checks
        if item.id == "ROCKWELL_STANDARD_INSTRUCTION_CATALOG"
    )
    assert check.status is StaticCheckStatus.WARN
    assert "classification-only" in check.summary
