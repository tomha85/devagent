from __future__ import annotations

from pathlib import Path

import pytest

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.plc_dispatch import detect_plc_vendor
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.siemens_tia_v1 import SiemensInputError, siemens_capability_profile


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _simple_scl(path: Path, *, expression: str = "Start AND Guard", extra_tag: str = "") -> Path:
    extra_decl = f"      {extra_tag} : Bool;" if extra_tag else ""
    return _write(
        path,
        f'''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
{extra_decl}
END_VAR
BEGIN
    Run := {expression};
END_ORGANIZATION_BLOCK
''',
    )


def test_siemens_project_only_scl_builds_bounded_ir_graph_fat_and_report(tmp_path: Path) -> None:
    project_path = _simple_scl(tmp_path / "Main.scl")
    assert detect_plc_vendor(project_path) == "SIEMENS"

    result = run_production_verification_v5(project_path)
    project = result.engineering.project
    profile = siemens_capability_profile(project)

    assert project.metadata.vendor == "Siemens"
    assert project.metadata.engineering_tool == "TIA Portal / Openness engineering export"
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["static_contract"] == "COMPLETE"
    assert project.st_statement_total == 1
    assert project.st_statement_semantic_count == 1
    assert len(project.output_logic) == 1
    assert project.output_logic[0].instruction == "ASSIGN_BOOL"
    assert {term.tag for term in project.output_logic[0].paths[0].terms} == {"Start", "Guard"}
    assert any(edge.kind == "DEPENDS_ON" and edge.source == "Run" and edge.target == "Start" for edge in result.engineering.graph.edges)
    assert any(edge.kind == "DEPENDS_ON" and edge.source == "Run" and edge.target == "Guard" for edge in result.engineering.graph.edges)

    tests = [item for item in result.engineering.fat_tests if item.output_tag == "Run"]
    assert len(tests) >= 2
    assert all(item.execution_status == "NOT_RUN" for item in tests)
    assert all(item.engineer_execution_required is True for item in tests)
    assert all(item.setup_steps and item.action_steps and item.why_required and item.evidence_required for item in tests)

    assert result.requirements == []
    assert result.stages[0].status.value == "PASS"
    assert "Validated Siemens TIA Portal engineering export bundle" in result.stages[0].summary
    assert result.stages[5].status.value == "SKIPPED"
    assert result.stages[6].status.value == "SKIPPED"
    assert result.readiness is not None
    assert result.readiness.status.value == "NOT_READY"

    report = render_production_report(result)
    assert "PROJECT_ONLY_ENGINEERING_REVIEW" in report
    assert "Customer requirements:** **NOT SUPPLIED" in report
    assert "## Semantic Coverage / Proof Boundary" in report
    assert "### Siemens TIA Export Inventory" in report
    assert "source/interface traceability" in report
    assert "Siemens V2" in report
    assert "DevAgent does not open proprietary `.ap*` / `.zap*` projects" in report
    assert "## Engineer FAT Procedures" in report


def test_siemens_boolean_requirement_can_be_statically_proven_without_runtime_claim(tmp_path: Path) -> None:
    project_path = _simple_scl(tmp_path / "Main.scl")
    requirements = _write(
        tmp_path / "requirements.md",
        "REQ-001: When Start=TRUE and Guard=TRUE, Run=TRUE.\n",
    )
    result = run_production_verification_v5(project_path, requirement_paths=[requirements])

    assert len(result.requirement_verification) == 1
    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert verification.linked_test_ids
    assert "bounded Siemens SCL assignment theorem" in verification.summary
    assert result.executions == []
    assert all(test.execution_status == "NOT_RUN" for test in result.engineering.fat_tests)
    assert result.readiness is not None
    assert result.readiness.status.value == "NOT_READY"


def test_siemens_boolean_requirement_conflict_is_deterministic(tmp_path: Path) -> None:
    project_path = _simple_scl(tmp_path / "Main.scl")
    requirements = _write(
        tmp_path / "requirements.md",
        "REQ-002: When Start=TRUE and Guard=FALSE, Run=TRUE.\n",
    )
    result = run_production_verification_v5(project_path, requirement_paths=[requirements])

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.CONFLICT
    assert any(risk.category == "REQUIREMENT" and risk.severity.value == "CRITICAL" for risk in result.risks)


def test_siemens_control_flow_stays_partial_and_becomes_runtime_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Controlled.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF Start THEN
        Run := Guard;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile(result.engineering.project)

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["partial_statements"] >= 2
    assert all(item.semantic_state is not PLCSemanticState.FULL for item in result.engineering.project.logic_statements)
    runtime = [item for item in result.engineering.fat_tests if item.scenario == "SCL_RUNTIME"]
    assert len(runtime) == 1
    assert runtime[0].output_tag == "Run"
    assert runtime[0].method == "RUNTIME_FAT_REQUIRED"
    assert runtime[0].execution_status == "NOT_RUN"
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in result.risks)


def test_siemens_multiple_source_writers_create_ownership_risk_and_optimization(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Writers.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
    Run := Guard;
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)

    writer_risks = [item for item in result.risks if item.category == "MULTIPLE_WRITERS"]
    assert len(writer_risks) == 1
    assert "Run" in writer_risks[0].title
    assert any(item.category == "OWNERSHIP" for item in result.optimizations)


def test_siemens_duplicate_scl_source_produces_duplication_recommendation(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Duplicate.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    A : Bool;
    B : Bool;
    X : Bool;
    Y : Bool;
END_VAR
BEGIN
    X := A AND B;
    X := A AND B;
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    assert any(item.category == "DUPLICATE_LOGIC" for item in result.optimizations)
    assert any(item.category == "DUPLICATION" for item in result.optimizations)


def test_siemens_openness_xml_imports_tags_interfaces_but_withholds_lad_network_behavior(tmp_path: Path) -> None:
    xml = _write(
        tmp_path / "Main.xml",
        '''
<Document>
  <SW.Tags.PlcTag ID="1"><AttributeList><Name>StartPB</Name><DataTypeName>Bool</DataTypeName><LogicalAddress>%I0.0</LogicalAddress></AttributeList></SW.Tags.PlcTag>
  <SW.Blocks.OB ID="2">
    <AttributeList>
      <Name>Main</Name><ProgrammingLanguage>LAD</ProgrammingLanguage>
      <Interface><Sections><Section Name="Temp"><Member Name="TempRun" Datatype="Bool" /></Section></Sections></Interface>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="3" CompositionName="CompileUnits">
        <AttributeList><ProgrammingLanguage>LAD</ProgrammingLanguage><NetworkSource><FlgNet /></NetworkSource></AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.OB>
</Document>
''',
    )
    engineering = analyze_plc_project(xml)
    project = engineering.project
    profile = siemens_capability_profile(project)

    assert engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(tag.name == "StartPB" and tag.data_type == "Bool" for tag in project.tags)
    assert any(tag.name == "TempRun" for tag in project.tags)
    assert len(project.routines) == 1
    assert project.routines[0].routine_type == "LAD"
    assert profile["opaque_statements"] == 1
    assert any("structurally imported" in warning for warning in project.warnings)


def test_siemens_regression_uses_same_vendor_baseline_and_detects_changed_boolean_logic(tmp_path: Path) -> None:
    old = _simple_scl(tmp_path / "old.scl")
    new = _simple_scl(tmp_path / "new.scl", expression="Start AND Guard AND Ready", extra_tag="Ready")
    result = run_production_verification_v5(new, baseline_path=old)

    assert result.baseline_sha256
    assert result.stages[11].status.value == "PASS"
    assert result.regression_changes
    assert any(change.change_type in {"LOGIC_CHANGED", "LOGIC_STATEMENT_CHANGED", "FAT_RECOMMENDATION_CHANGED", "FAT_RECOMMENDATION_ADDED"} for change in result.regression_changes)


def test_siemens_proprietary_tia_project_archive_is_rejected_with_export_guidance(tmp_path: Path) -> None:
    archive = _write(tmp_path / "Machine.zap20", "not parsed")
    with pytest.raises(SiemensInputError, match="Openness/XML|GenerateSource"):
        analyze_plc_project(archive)
