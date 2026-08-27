from __future__ import annotations

from pathlib import Path

import pytest

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.semantic_coverage_report import render_semantic_coverage_section
from devagent.plc.siemens_closeout_v9 import _source_manifest, siemens_capability_profile_v9
from devagent.plc.siemens_identity_types_v8 import _preflight, siemens_capability_profile_v8
from devagent.plc.siemens_tia_v1 import SiemensInputError, _MAX_TOTAL_BYTES


def _write(path: Path, text: str, *, crlf: bool = False) -> Path:
    payload = text.strip() + "\n"
    if crlf:
        path.write_bytes(payload.replace("\n", "\r\n").encode("utf-8"))
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _basic_source() -> str:
    return """
ORGANIZATION_BLOCK Main
VAR
    Start : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_ORGANIZATION_BLOCK
"""


def test_public_bootstrap_reaches_v9_and_keeps_complete_simple_project_full(tmp_path: Path) -> None:
    source = _write(tmp_path / "main.scl", _basic_source())
    result = analyze_plc_project(source)
    profile = siemens_capability_profile_v9(result.project)

    assert result.project.metadata.schema_revision == "SIEMENS-TIA-EXPORT-V9"
    assert profile["schema"] == "devagent-siemens-tia-capability-v9"
    assert profile["coverage_accounting_complete"] is True
    assert profile["support_contract"] == "FULL"
    assert profile["support_partial"] == 0
    assert profile["support_opaque"] == 0
    assert profile["support_protected"] == 0
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED


def test_v8_canonicalizes_quoted_db_udt_struct_array_enum_and_local_scope(tmp_path: Path) -> None:
    _write(
        tmp_path / "Types.udt",
        """
TYPE "AxisData"
STRUCT
    Enabled : Bool;
    Speed : Real;
    Samples : ARRAY[0..3] OF DInt;
END_STRUCT;
END_TYPE

TYPE MachineState : (Idle, Running, Fault);
END_TYPE
""",
    )
    _write(
        tmp_path / "Machine.db",
        """
DATA_BLOCK "Machine DB"
VAR
    Axis : "AxisData";
    State : MachineState;
END_VAR
BEGIN
END_DATA_BLOCK
""",
    )
    _write(
        tmp_path / "Worker.scl",
        """
FUNCTION_BLOCK "Worker Block"
VAR_INPUT
    "Run Cmd" : Bool;
END_VAR
VAR
    Axis : "AxisData";
    State : MachineState;
END_VAR
BEGIN
    "Machine DB".Axis.Enabled := "Run Cmd";
END_FUNCTION_BLOCK
""",
    )

    result = analyze_plc_project(tmp_path)
    project = result.project
    v8 = siemens_capability_profile_v8(project)
    facts = project._siemens_v8_identity_facts

    assert project.metadata.schema_revision == "SIEMENS-TIA-EXPORT-V9"
    assert v8["udt_types"] + v8["struct_types"] >= 1
    assert v8["array_types"] >= 1
    assert v8["enum_types"] >= 1

    by_display = {(item.scope.casefold(), item.display_path.casefold()): item for item in facts.symbols}
    assert ("controller", "machine db.axis.enabled") in by_display
    assert ("program:worker block", "axis.enabled") in by_display
    assert any(item.display_path.casefold().endswith("samples[*]") for item in facts.symbols)

    write = next(
        item for item in facts.bindings
        if item.access == "WRITE" and item.raw_ref.casefold() == "machine db.axis.enabled"
    )
    assert write.semantic_state is PLCSemanticState.FULL
    assert write.canonical_display.casefold() == "controller::machine db.axis.enabled"


def test_v8_local_symbol_shadows_controller_symbol_deterministically(tmp_path: Path) -> None:
    _write(
        tmp_path / "Global.db",
        """
DATA_BLOCK GlobalDB
VAR
    Run : Bool;
END_VAR
BEGIN
END_DATA_BLOCK
""",
    )
    _write(
        tmp_path / "Main.scl",
        """
ORGANIZATION_BLOCK Main
VAR
    Run : Bool;
    Start : Bool;
END_VAR
BEGIN
    Run := Start;
END_ORGANIZATION_BLOCK
""",
    )
    result = analyze_plc_project(tmp_path)
    facts = result.project._siemens_v8_identity_facts
    write = next(item for item in facts.bindings if item.access == "WRITE" and item.raw_ref == "Run")
    assert write.resolution == "LOCAL_SHADOW"
    assert write.canonical_display.casefold() == "program:main::run"


def test_v8_single_file_100_mib_bound_is_checked_before_analyzer_reads(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed.scl"
    with allowed.open("wb") as handle:
        handle.truncate(_MAX_TOTAL_BYTES)
    _root, files = _preflight(allowed)
    assert files[0][0].stat().st_size == _MAX_TOTAL_BYTES

    oversized = tmp_path / "oversized.scl"
    with oversized.open("wb") as handle:
        handle.truncate(_MAX_TOTAL_BYTES + 1)
    with pytest.raises(SiemensInputError, match="100 MiB production limit"):
        _preflight(oversized)


def test_v9_manifest_is_deterministic_and_reports_unicode_crlf(tmp_path: Path) -> None:
    _write(tmp_path / "b.scl", _basic_source().replace("Main", '"Mäin"'), crlf=True)
    _write(tmp_path / "a.scl", _basic_source().replace("Main", "Other"))

    first = _source_manifest(tmp_path)
    second = _source_manifest(tmp_path)
    assert first == second
    assert first[0] == 2
    assert first[1] > 0
    assert len(first[2]) == 64
    assert "CRLF" in first[3]
    assert first[4] is True


def test_v9_opaque_fbd_region_cannot_disappear_from_support_contract_or_report(tmp_path: Path) -> None:
    xml = _write(
        tmp_path / "Opaque.xml",
        """
<Document xmlns="urn:siemens:engineering:test:v20">
  <SW.Blocks.OB ID="1">
    <AttributeList>
      <Name>Main</Name>
      <ProgrammingLanguage>FBD</ProgrammingLanguage>
      <Interface><Sections>
        <Section Name="Temp"><Member Name="Run" Datatype="Bool" /></Section>
      </Sections></Interface>
    </AttributeList>
    <ObjectList>
      <SW.Blocks.CompileUnit ID="2" CompositionName="CompileUnits">
        <AttributeList>
          <ProgrammingLanguage>FBD</ProgrammingLanguage>
          <NetworkSource>
            <FlgNet>
              <Parts>
                <Access Scope="LocalVariable" UId="21"><Symbol><Component Name="Run" /></Symbol></Access>
                <Part Name="UnsupportedProductionPart" UId="22" />
              </Parts>
              <Wires />
            </FlgNet>
          </NetworkSource>
        </AttributeList>
      </SW.Blocks.CompileUnit>
    </ObjectList>
  </SW.Blocks.OB>
</Document>
""",
    )

    production = run_production_verification_v5(xml)
    profile = siemens_capability_profile_v9(production.engineering.project)
    semantic = render_semantic_coverage_section(production.engineering.project)

    assert profile["coverage_accounting_complete"] is True
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["support_opaque"] + profile["support_partial"] >= 1
    assert production.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert "Siemens V9 Support Contract / Commercial Closeout" in semantic
    assert "Explicit unsupported / runtime-required regions" in semantic
    assert "UnsupportedProductionPart" in semantic or "OPAQUE" in semantic
    assert any(item.kind == "SIEMENS_SUPPORT_REGION_V9" for item in production.evidence)
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in production.risks)


def test_v9_support_contract_accounts_for_every_imported_statement(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "mixed.scl",
        """
ORGANIZATION_BLOCK Main
VAR
    A : Bool;
    B : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := A AND B;
    IF A THEN
        Run := B;
    END_IF;
END_ORGANIZATION_BLOCK
""",
    )
    result = analyze_plc_project(source)
    facts = result.project._siemens_v9_closeout_facts
    executable_regions = [r for r in facts.support.regions if r.region_type == "EXECUTABLE_STATEMENT"]
    assert facts.support.accounting_complete is True
    assert facts.support.missing_statement_ids == ()
    assert len(executable_regions) == len(result.project.logic_statements)
    assert {r.source_evidence_id for r in executable_regions} == {s.id for s in result.project.logic_statements}
