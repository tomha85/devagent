from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from devagent.plc import run_production_verification_v5
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_report import render_production_report
from devagent.plc.report_contract_v1 import build_report_contract
from devagent.plc.top_engineering_risks_v1 import render_top_engineering_risks


def _schneider_project(tmp_path: Path) -> Path:
    path = tmp_path / "ReportContract.xst"
    path.write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="ReportContract" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
IF Gate THEN
Run := Start;
END_IF;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Gate" typeName="BOOL" />
    <variables name="Start" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
''',
        encoding="utf-8",
    )
    return path


def test_project_only_report_contract_separates_analysis_from_release_readiness(tmp_path: Path) -> None:
    result = run_production_verification_v5(_schneider_project(tmp_path))
    contract = build_report_contract(result)
    report = render_production_report(result)

    assert contract["schema"] == "devagent-plc-report-contract-v1"
    assert contract["review_mode"] == "PROJECT_ONLY_ENGINEERING_REVIEW"
    assert contract["requirements"]["scope"] == "NOT_PROVIDED"
    assert contract["requirements"]["total"] == 0
    assert contract["release_score_is_not_analysis_quality"] is True
    assert contract["contract_status"] == "PASS"

    assert "### Independent Decision Scorecard" in report
    assert "release-readiness score is a policy/evidence gate, not a score of DevAgent analysis quality" in report
    assert "| Requirement verification | NOT PROVIDED |" in report
    assert "| Report consistency contract | PASS |" in report
    assert "Requirement coverage incomplete" not in render_top_engineering_risks(result)


def test_schneider_report_uses_current_capability_contract_not_stale_v1_v2_wording(tmp_path: Path) -> None:
    result = run_production_verification_v5(_schneider_project(tmp_path))
    report = render_production_report(result)

    assert "### Current Schneider Capability Contract" in report
    assert "currently installed qualified Schneider capability stack" in report
    assert "Schneider Control Expert V1 separates" not in report
    assert "### Explicit Schneider V1 Boundaries" not in report

    semantic_risks = [item for item in result.risks if item.category == "SEMANTIC_COVERAGE"]
    assert semantic_risks
    assert all("Schneider V2 analyzer" not in item.summary for item in semantic_risks)
    assert all("current installed Schneider Control Expert analyzer stack" in item.summary for item in semantic_risks)


def test_top_risk_grouping_uses_explicit_requirement_category_and_vendor_root_cause() -> None:
    risk = RiskFinding(
        id="RISK-CALL-demo",
        category="CALL_BINDING",
        title="Schneider unresolved call binding",
        severity=Severity.HIGH,
        summary="Schneider Control Expert call target is unresolved.",
        consequence="Requirement and FAT traceability may depend on the unresolved call.",
        recommendation="Resolve the call binding or execute FAT.",
        evidence_ids=("CALL-1",),
    )
    rendered = render_top_engineering_risks(SimpleNamespace(risks=[risk]))

    assert "Unresolved/ambiguous Schneider call bindings" in rendered
    assert "Requirement coverage incomplete" not in rendered
