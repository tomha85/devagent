from __future__ import annotations

from pathlib import Path

import pytest

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_closeout_v9 import _source_manifest, schneider_capability_profile_v9
from devagent.plc.schneider_control_expert_v1 import SchneiderInputError


def _write(path: Path, text: str, *, crlf: bool = False) -> Path:
    payload = text.strip() + "\n"
    if crlf:
        path.write_bytes(payload.replace("\n", "\r\n").encode("utf-8"))
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _xst(body: str, *, name: str = "Main", dtd: str = "41", product: str = "EcoStruxure Control Expert V16") -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="{product}" DTDVersion="{dtd}" />
  <contentHeader name="SchneiderV9" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <STSource>
{body.strip()}
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
    <variables name="Fault" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
'''


def _xsf() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<SFCExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9" version="1.0" />
  <program>
    <identProgram name="Sequence" type="section" task="MAST" />
    <SFCSource>
      <step name="S0" />
    </SFCSource>
  </program>
</SFCExchangeFile>
'''


def _protected_xdb() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<FBExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9" version="1.0" />
  <FBSource nameOfFBType="SECRET_DFB" protected="true">
    <inputParameters><variables name="Start" typeName="BOOL" /></inputParameters>
    <outputParameters><variables name="Run" typeName="BOOL" /></outputParameters>
  </FBSource>
</FBExchangeFile>
'''


def test_v9_simple_project_has_full_support_accounting_but_external_gate_pending(tmp_path: Path) -> None:
    source = _write(tmp_path / "Main.xst", _xst("Run := Start;"))
    result = analyze_plc_project(source)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v9"
    assert profile["coverage_accounting_complete"] is True
    assert profile["support_contract"] == "FULL"
    assert profile["support_partial"] == 0
    assert profile["support_opaque"] == 0
    assert profile["support_protected"] == 0
    assert profile["real_control_expert_export_corpus"] == "PENDING_EXTERNAL_EVIDENCE"
    assert profile["simulator_hil_real_plc_execution"] == "NOT_EXECUTED"
    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert any(
        check.id == "SCHNEIDER_V9_EXTERNAL_EXPORT_CORPUS"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.static_checks
    )


def test_v9_manifest_is_deterministic_and_reports_unicode_crlf_and_suffixes(tmp_path: Path) -> None:
    _write(tmp_path / "b.xst", _xst("Run := Start;", name="Mäin"), crlf=True)
    _write(tmp_path / "a.xst", _xst("Run := Start;", name="Other"))

    first = _source_manifest(tmp_path)[1]
    second = _source_manifest(tmp_path)[1]

    assert first == second
    assert first.source_files == 2
    assert first.source_bytes > 0
    assert len(first.deterministic_manifest_sha256) == 64
    assert "CRLF" in first.line_endings
    assert first.unicode_present is True
    assert dict(first.suffix_counts)[".xst"] == 2


def test_v9_opaque_sfc_cannot_disappear_from_support_contract_report_or_evidence(tmp_path: Path) -> None:
    source = _write(tmp_path / "Sequence.xsf", _xsf())
    production = run_production_verification_v5(source)
    profile = schneider_capability_profile_v9(production.engineering.project)
    report = render_production_report(production)

    assert profile["coverage_accounting_complete"] is True
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["support_opaque"] >= 1
    assert production.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert "### Schneider V9 Support Contract / Commercial Closeout" in report
    assert "Explicit unsupported / runtime-required regions" in report
    assert any(item.kind == "SCHNEIDER_SUPPORT_REGION_V9" for item in production.evidence)
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in production.risks)


def test_v9_protected_dfb_is_explicit_protected_region(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    root.mkdir()
    _write(root / "Main.xst", _xst("Run := Start;"))
    _write(root / "Secret.xdb", _protected_xdb())

    production = run_production_verification_v5(root)
    project = production.engineering.project
    profile = schneider_capability_profile_v9(project)
    facts = project._schneider_v9_closeout_facts

    assert profile["support_protected"] >= 1
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"
    assert any(region.region_type == "PROTECTED_DFB" for region in facts.support.regions)


def test_v9_metadata_revision_mismatch_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "mismatch"
    root.mkdir()
    _write(root / "Main.xst", _xst("Run := Start;", name="Main", dtd="41"))
    _write(root / "Aux.xst", _xst("Run := Start;", name="Aux", dtd="42"))

    result = analyze_plc_project(root)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["export_metadata_consistent"] is False
    assert set(profile["dtd_versions"]) == {"41", "42"}
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(
        check.id == "SCHNEIDER_V9_EXPORT_METADATA"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.static_checks
    )


def test_v9_duplicate_section_identity_is_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "duplicate"
    root.mkdir()
    _write(root / "A.xst", _xst("Run := Start;", name="Main"))
    _write(root / "B.xst", _xst("Run := Start;", name="Main"))

    result = analyze_plc_project(root)
    profile = schneider_capability_profile_v9(result.project)

    assert profile["duplicate_section_keys"]
    assert profile["support_contract"] == "PARTIAL_FAIL_CLOSED"
    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v9_malformed_control_expert_xml_is_rejected_before_closeout(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Broken.xst",
        '<STExchangeFile><program><identProgram name="Main"/><STSource>Run := Start;</program>',
    )
    with pytest.raises(SchneiderInputError, match="Invalid Control Expert XML"):
        analyze_plc_project(source)


def test_v9_support_contract_accounts_for_every_imported_statement(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Mixed.xst",
        _xst(
            '''
Run := Start;
IF Fault THEN
Run := FALSE;
END_IF
'''
        ),
    )
    result = analyze_plc_project(source)
    facts = result.project._schneider_v9_closeout_facts
    executable = [item for item in facts.support.regions if item.region_type == "EXECUTABLE_STATEMENT"]

    assert facts.support.accounting_complete is True
    assert facts.support.missing_statement_ids == ()
    assert len(executable) == len(result.project.logic_statements)
    assert {item.source_evidence_id for item in executable} == {item.id for item in result.project.logic_statements}
