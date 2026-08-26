from __future__ import annotations

import json
from pathlib import Path

import pytest

from devagent import entrypoint
from devagent.plc.analysis import analyze_rockwell_l5x
from devagent.plc.cli import main as plc_main
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.rockwell_l5x import L5XError, parse_full_project_l5x


FULL_PROJECT_L5X = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="DemoController" TargetType="Controller" ContainsContext="true">
  <Controller Use="Target" Name="DemoController" ProcessorType="1756-L83E" MajorRev="36" MinorRev="11">
    <DataTypes><DataType Name="MotorType" Family="NoFamily" /></DataTypes>
    <Modules><Module Name="Local" CatalogNumber="1756-L83E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="StartPB" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" />
      <Tag Name="GuardClosed" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" />
      <Tag Name="MotorFault" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" />
      <Tag Name="MotorRun" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" />
      <Tag Name="MotorLatched" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" />
    </Tags>
    <Programs><Program Name="MainProgram">
      <Tags><Tag Name="LocalPermissive" TagType="Base" DataType="BOOL" ExternalAccess="Read/Write" /></Tags>
      <Routines><Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0" Type="N"><Comment>Motor start command</Comment><Text><![CDATA[XIC(StartPB)XIC(GuardClosed)XIO(MotorFault)OTE(MotorRun);]]></Text></Rung>
        <Rung Number="1" Type="N"><Text><![CDATA[XIC(MotorRun)OTL(MotorLatched);]]></Text></Rung>
      </RLLContent></Routine></Routines>
    </Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" Priority="10" Rate="10" /></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _write_l5x(tmp_path: Path, content: str = FULL_PROJECT_L5X, name: str = "Machine.L5X") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_full_project_l5x_normalizes_inventory_and_provenance(tmp_path: Path) -> None:
    project = parse_full_project_l5x(_write_l5x(tmp_path))
    assert project.metadata.vendor == "Rockwell Automation"
    assert project.metadata.full_project is True
    assert project.metadata.controller_name == "DemoController"
    assert project.metadata.processor_type == "1756-L83E"
    assert len(project.tags) == 6
    assert len(project.programs) == 1
    assert len(project.routines) == 1
    assert len(project.rungs) == 2
    assert project.rungs[0].reads == ("GuardClosed", "MotorFault", "StartPB")
    assert project.rungs[0].writes == ("MotorRun",)
    assert project.rungs[0].source.locator == "DemoController / MainProgram / MainRoutine / Rung 0"
    assert project.instruction_semantic_coverage == 1.0


def test_dependency_graph_and_fat_tests_are_source_traceable(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_l5x(tmp_path))
    dependencies = {(edge.source, edge.target, edge.kind) for edge in result.graph.edges if edge.kind == "DEPENDS_ON"}
    rung0 = result.project.rungs[0]
    assert ("MotorRun", "StartPB", "DEPENDS_ON") in dependencies
    assert ("MotorRun", "GuardClosed", "DEPENDS_ON") in dependencies
    assert ("MotorRun", "MotorFault", "DEPENDS_ON") in dependencies
    motor_test = next(test for test in result.fat_tests if test.output_tag == "MotorRun" and test.scenario == "POSITIVE_PATH")
    assert motor_test.preconditions == {"GuardClosed": True, "MotorFault": False, "StartPB": True}
    assert motor_test.execution_status == "NOT_RUN"
    assert motor_test.source == rung0.source
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert any(check.id == "SIMULATOR_EXECUTION" and check.status is StaticCheckStatus.NOT_PROVEN for check in result.static_checks)


def test_component_l5x_is_rejected_fail_closed(tmp_path: Path) -> None:
    component = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="MainRoutine" TargetType="Routine">
  <Routine Use="Target" Name="MainRoutine" Type="RLL"><RLLContent /></Routine>
</RSLogix5000Content>
"""
    with pytest.raises(L5XError, match="not a full-project Controller export"):
        parse_full_project_l5x(_write_l5x(tmp_path, component, "Routine.L5X"))


def test_l5x_with_dtd_or_entity_is_rejected_before_xml_parse(tmp_path: Path) -> None:
    malicious = """<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY example "unsafe">]>
<RSLogix5000Content TargetType="Controller"><Controller Name="Demo" /></RSLogix5000Content>
"""
    with pytest.raises(L5XError, match="DTD/entities"):
        parse_full_project_l5x(_write_l5x(tmp_path, malicious))


def test_supported_st_logic_is_normalized_in_v2(tmp_path: Path) -> None:
    mixed = FULL_PROJECT_L5X.replace(
        "</Routines>",
        '<Routine Name="SequenceST" Type="ST"><STContent><Line Number="0">MotorRun := StartPB;</Line></STContent></Routine></Routines>',
        1,
    )
    result = analyze_rockwell_l5x(_write_l5x(tmp_path, mixed))
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    st = next(check for check in result.static_checks if check.id == "STRUCTURED_TEXT_SEMANTICS")
    assert st.status is StaticCheckStatus.PASS
    assert result.project.st_statement_semantic_count == 1
    assert not any("unsupported routine types: ST" in item for item in result.limitations)


def test_unknown_instruction_semantics_reduce_claimed_coverage(tmp_path: Path) -> None:
    unknown = FULL_PROJECT_L5X.replace(
        "XIC(MotorRun)OTL(MotorLatched);",
        "XIC(MotorRun)VendorSpecificInstruction(MotorLatched);",
    )
    result = analyze_rockwell_l5x(_write_l5x(tmp_path, unknown))
    assert result.project.instruction_semantic_coverage < 1.0
    assert result.project.unknown_instruction_names == ["VendorSpecificInstruction"]
    assert result.graph.unknown_instruction_names == ["VendorSpecificInstruction"]
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any("VendorSpecificInstruction" in item for item in result.limitations)


def test_branched_rung_models_output_specific_dependencies_without_cross_branch_edges(tmp_path: Path) -> None:
    branched = FULL_PROJECT_L5X.replace(
        "XIC(StartPB)XIC(GuardClosed)XIO(MotorFault)OTE(MotorRun);",
        "[XIC(StartPB)OTE(MotorRun),XIC(GuardClosed)OTE(MotorLatched)];",
    )
    result = analyze_rockwell_l5x(_write_l5x(tmp_path, branched))
    deps = {(edge.source, edge.target) for edge in result.graph.edges if edge.kind == "DEPENDS_ON"}
    assert ("MotorRun", "StartPB") in deps
    assert ("MotorLatched", "GuardClosed") in deps
    assert ("MotorRun", "GuardClosed") not in deps
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    branch_check = next(check for check in result.static_checks if check.id == "BRANCH_DEPENDENCY_SEMANTICS")
    assert branch_check.status is StaticCheckStatus.PASS
    assert any(test.source == result.project.rungs[0].source for test in result.fat_tests)


def test_protected_aoi_forces_partial_verification(tmp_path: Path) -> None:
    protected = FULL_PROJECT_L5X.replace(
        "<AddOnInstructionDefinitions />",
        """<AddOnInstructionDefinitions>
      <AddOnInstructionDefinition Name="ProtectedAOI">
        <Parameters><Parameter Name="Enable" Usage="Input" DataType="BOOL" /><Parameter Name="Command" Usage="Output" DataType="BOOL" /></Parameters>
        <EncodedSource>opaque</EncodedSource>
      </AddOnInstructionDefinition>
    </AddOnInstructionDefinitions>""",
    ).replace("XIC(MotorRun)OTL(MotorLatched);", "ProtectedAOI(MotorRun,MotorLatched);")
    result = analyze_rockwell_l5x(_write_l5x(tmp_path, protected))
    assert result.project.aois[0].source_protected is True
    assert result.project.instruction_semantic_coverage == 1.0
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any("Add-On Instruction" in item and "NOT_PROVEN" in item for item in result.limitations)


def test_full_project_without_parsed_logic_is_not_statically_verified(tmp_path: Path) -> None:
    empty = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="EmptyController" TargetType="Controller">
  <Controller Use="Target" Name="EmptyController" ProcessorType="1756-L83E" MajorRev="36" MinorRev="11"><Tags /><Programs /><Tasks /></Controller>
</RSLogix5000Content>
"""
    result = analyze_rockwell_l5x(_write_l5x(tmp_path, empty, "Empty.L5X"))
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    provenance = next(check for check in result.static_checks if check.id == "SOURCE_PROVENANCE")
    coverage = next(check for check in result.static_checks if check.id == "LOGIC_SEMANTIC_COVERAGE")
    assert provenance.status is StaticCheckStatus.NOT_PROVEN
    assert coverage.status is StaticCheckStatus.NOT_PROVEN
    assert any("controller behavior remains NOT_PROVEN" in item for item in result.limitations)


def test_plc_cli_writes_machine_readable_evidence_and_fat_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _write_l5x(tmp_path)
    output = tmp_path / "plc-output"
    assert plc_main([str(project), "--output-dir", str(output)]) == 2
    for name in (
        "canonical_ir.json",
        "dependency_graph.json",
        "fat_tests.json",
        "static_verification.json",
        "release_readiness.json",
        "pipeline_stages.json",
        "run_manifest.json",
        "fat_report.md",
    ):
        assert (output / name).is_file()
    canonical = json.loads((output / "canonical_ir.json").read_text(encoding="utf-8"))
    assert canonical["metadata"]["controller_name"] == "DemoController"
    report = (output / "fat_report.md").read_text(encoding="utf-8")
    assert "STATICALLY_VERIFIED" in report
    assert "NOT_READY" in report
    stdout = capsys.readouterr().out
    assert "[ 1/15] PROJECT VALIDATION" in stdout
    assert "[ 9/15] TEST EXECUTION" in stdout
    assert "[15/15] RELEASE READINESS" in stdout
    assert "NOT_RUN" in stdout


def test_entrypoint_delegates_existing_software_cli_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    def fake_software_main(argv) -> int:
        calls.append(list(argv)); return 17
    monkeypatch.setattr("devagent.cli.main", fake_software_main)
    assert entrypoint.main(["--repo", "/tmp/repo", "add feature"]) == 17
    assert calls == [["--repo", "/tmp/repo", "add feature"]]


def test_entrypoint_routes_only_plc_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    def fake_plc_main(argv) -> int:
        calls.append(list(argv)); return 23
    monkeypatch.setattr("devagent.plc.cli.main", fake_plc_main)
    assert entrypoint.main(["plc", "Machine.L5X", "--no-write"]) == 23
    assert calls == [["Machine.L5X", "--no-write"]]
