from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc import production_report
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest
from devagent.plc.semantic_coverage_report import (
    render_production_report as coverage_render_production_report,
    render_semantic_coverage_section,
)


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Coverage" TargetType="Controller">
  <Controller Name="Coverage" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="Axis1" TagType="Base" DataType="AXIS_CIP_DRIVE" />
      <Tag Name="JogControl" TagType="Base" DataType="MOTION_INSTRUCTION" />
      <Tag Name="STOut" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[MOV(Source,Dest);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[MAJ(Axis1,JogControl,1,100.0);]]></Text></Rung>
        <Rung Number="3"><Text><![CDATA[VendorMystery(Source,Dest);]]></Text></Rung>
      </RLLContent></Routine>
      <Routine Name="Sequence" Type="ST"><STContent>
        <Line Number="0"><![CDATA[STOut := Start;]]></Line>
      </STContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "Coverage.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _row(manifest, name: str):
    return next(item for item in manifest["instructions"] if item["instruction"] == name)


def test_manifest_separates_proof_structural_partial_and_unmodeled_semantics(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    manifest = build_semantic_coverage_manifest(engineering.project)

    assert manifest["schema"] == "devagent-plc-semantic-coverage-v1"
    assert manifest["project"]["controller"] == "Coverage"
    assert _row(manifest, "XIC")["levels"] == {"DETERMINISTIC_PATH": 1}
    assert _row(manifest, "OTE")["levels"] == {"DETERMINISTIC_PATH": 1}
    assert _row(manifest, "MOV")["levels"] == {"BOUNDED_DETERMINISTIC": 1}
    assert _row(manifest, "MAJ")["levels"] == {"PARTIAL": 1}
    assert _row(manifest, "VENDORMYSTERY")["levels"] == {"UNMODELED": 1}

    summary = manifest["instruction_summary"]
    assert summary["scope"] == "PROGRAM_RLL"
    assert summary["total_occurrences"] == 5
    assert summary["deterministic_occurrences"] == 3
    assert summary["structural_only_occurrences"] == 0
    assert summary["partial_occurrences"] == 1
    assert summary["unmodeled_occurrences"] == 1
    assert summary["deterministic_pct"] == 60.0
    assert summary["structural_or_better_pct"] == 60.0
    assert summary["unmodeled_pct"] == 20.0
    assert manifest["action_semantics"]["modeled_actions"] == 1
    assert manifest["stateful_runtime_semantics"]["modeled_occurrences"] == 0


def test_manifest_reports_inventory_language_and_project_boundaries_without_overclaiming(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    manifest = build_semantic_coverage_manifest(engineering.project)

    inventory = manifest["inventory"]
    rll = manifest["language_summary"]["rll"]
    st = manifest["language_summary"]["structured_text"]
    aoi = manifest["language_summary"]["aoi"]
    boundaries = manifest["project_boundaries"]

    assert inventory["tags"] == 7
    assert inventory["tasks"] == 1
    assert inventory["scheduled_program_entries"] == 1
    assert inventory["programs"] == 1
    assert inventory["routines"] == 2
    assert inventory["program_rll_rungs"] == 4
    assert inventory["structured_text_statements"] == 1
    assert inventory["aois"] == 0
    assert inventory["output_logic_objects"] == 1

    assert rll["program_rungs"] == 4
    assert rll["deterministic_boolean_rungs"] == 1
    assert rll["bounded_action_rungs"] == 1
    assert st["statements"] == 1
    # Sequence is discovered in the project inventory but is not the Main routine
    # and is never reached by a JSR. The final semantic state therefore stays
    # partial/unreachable and must not be reported as parser-level semantic proof.
    assert st["reachable_full_dataflow_statements"] == 0
    assert st["partial_or_unreachable_statements"] == 1
    assert st["reachable_full_dataflow_pct"] == 0.0
    assert "parser_semantic_count" not in st
    assert aoi["internal_rll_statements"] == 0
    assert aoi["internal_st_statements"] == 0
    assert "MAJ" in boundaries["partially_modeled_instruction_names"]
    assert "VENDORMYSTERY" in boundaries["unmodeled_instruction_names"]
    assert boundaries["warnings"] == engineering.project.warnings
    assert "Structural coverage means reads/writes/calls are normalized" in manifest["trust_note"]
    assert "Unreachable" in manifest["trust_note"]


def test_manifest_is_bound_to_exact_project_hash(tmp_path: Path) -> None:
    path = _write_project(tmp_path)
    engineering = analyze_rockwell_l5x(path)
    manifest = build_semantic_coverage_manifest(engineering.project)

    assert manifest["project"]["source_sha256"] == engineering.project.metadata.source_sha256


def test_fat_report_semantic_section_uses_same_project_manifest(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    section = render_semantic_coverage_section(engineering.project)

    assert "## Semantic Coverage / Proof Boundary" in section
    assert "### Project Inventory" in section
    assert "Tags: **7**" in section
    assert "Programs: **1**" in section
    assert "Routines: **2**" in section
    assert "Program RLL bounded deterministic behavior coverage: **60.0%** (3/5 instruction occurrences)" in section
    assert "| MOV | 1 | BOUNDED_DETERMINISTIC=1 |" in section
    assert "| MAJ | 1 | PARTIAL=1 |" in section
    assert "| VENDORMYSTERY | 1 | UNMODELED=1 |" in section
    assert "ST statements discovered: **1**" in section
    assert "Reachable FULL ST dataflow: **0**/1 (0.0%)" in section
    assert "parser-level recognition" not in section
    assert "Analysis warnings:" in section


def test_semantic_report_augmentation_is_installed_before_cli_imports() -> None:
    assert production_report.render_production_report is coverage_render_production_report
