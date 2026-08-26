from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest


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
    assert _row(manifest, "MOV")["levels"] == {"STRUCTURAL_RW": 1}
    assert _row(manifest, "MAJ")["levels"] == {"PARTIAL": 1}
    assert _row(manifest, "VENDORMYSTERY")["levels"] == {"UNMODELED": 1}

    summary = manifest["instruction_summary"]
    assert summary["total_occurrences"] == 5
    assert summary["deterministic_occurrences"] == 2
    assert summary["structural_only_occurrences"] == 1
    assert summary["partial_occurrences"] == 1
    assert summary["unmodeled_occurrences"] == 1
    assert summary["deterministic_pct"] == 40.0
    assert summary["structural_or_better_pct"] == 60.0
    assert summary["unmodeled_pct"] == 20.0


def test_manifest_reports_language_and_project_boundaries_without_overclaiming(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    manifest = build_semantic_coverage_manifest(engineering.project)

    rll = manifest["language_summary"]["rll"]
    st = manifest["language_summary"]["structured_text"]
    boundaries = manifest["project_boundaries"]

    assert rll["rungs"] == 4
    assert rll["deterministic_boolean_rungs"] == 1
    assert st["statements"] == 1
    # Sequence is not the program Main routine and is never reached by a JSR.
    # Parser-level ST recognition is retained, but proof-grade reachable coverage
    # must fail closed rather than count dead/uninvoked code as understood.
    assert st["parser_semantic_count"] == 1
    assert st["reachable_full_dataflow_statements"] == 0
    assert st["partial_or_unreachable_statements"] == 1
    assert st["reachable_full_dataflow_pct"] == 0.0
    assert "MAJ" in boundaries["partially_modeled_instruction_names"]
    assert "VENDORMYSTERY" in boundaries["unmodeled_instruction_names"]
    assert "Structural coverage means reads/writes/calls are normalized" in manifest["trust_note"]
    assert "Unreachable" in manifest["trust_note"]


def test_manifest_is_bound_to_exact_project_hash(tmp_path: Path) -> None:
    path = _write_project(tmp_path)
    engineering = analyze_rockwell_l5x(path)
    manifest = build_semantic_coverage_manifest(engineering.project)

    assert manifest["project"]["source_sha256"] == engineering.project.metadata.source_sha256
