from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.plc_dispatch import detect_plc_vendor
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_control_expert_v1 import schneider_capability_profile


ST_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderDemo" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>Run := Start AND NOT Stop;</STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Stop" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""

PARTIAL_ST = """<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert" DTDVersion="41" />
  <contentHeader name="PartialDemo" version="1.0" />
  <program>
    <identProgram name="Sequence" type="section" task="MAST" />
    <STSource>
IF Start THEN
    Run := Guard;
END_IF;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Guard" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
"""

LD_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<LDExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert" DTDVersion="41" />
  <contentHeader name="LadderDemo" version="1.0" />
  <program>
    <identProgram name="Motor" type="section" task="MAST" />
    <LDSource nbColumns="11"><networkLD><typeLine>
      <contact typeContact="openContact" contactVariableName="Start" />
      <contact typeContact="closedContact" contactVariableName="Stop" />
      <HLink nbCells="8" />
      <coil typeCoil="coil" coilVariableName="Run" />
    </typeLine></networkLD></LDSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" />
    <variables name="Stop" typeName="BOOL" />
    <variables name="Run" typeName="BOOL" />
  </dataBlock>
</LDExchangeFile>
"""

FBD_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<FBDExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert" DTDVersion="41" />
  <contentHeader name="FBDDemo" version="1.0" />
  <program>
    <identProgram name="TimerLogic" type="section" task="MAST" />
    <FBDSource nbRows="24" nbColumns="36"><networkFBD><FFBBlock instanceName="Delay" typeName="TON" /></networkFBD></FBDSource>
  </program>
  <dataBlock><variables name="Delay" typeName="TON" /></dataBlock>
</FBDExchangeFile>
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_schneider_st_builds_ir_graph_fat_and_vendor_report(tmp_path: Path) -> None:
    source = _write(tmp_path / "Main.XST", ST_PROJECT)
    assert detect_plc_vendor(source) == "SCHNEIDER"
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile(project)

    assert project.metadata.vendor == "Schneider Electric"
    assert project.metadata.controller_name == "SchneiderDemo"
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["static_contract"] == "COMPLETE"
    assert {tag.name for tag in project.tags} >= {"Start", "Stop", "Run"}
    assert len(project.logic_statements) == 1
    assert project.logic_statements[0].semantic_state is PLCSemanticState.FULL
    assert project.output_logic[0].output_tag == "Run"
    assert any(edge.kind == "DEPENDS_ON" and edge.source.endswith("Run") for edge in result.engineering.graph.edges)
    assert result.engineering.fat_tests
    assert all(test.id.startswith("FAT-SCHNEIDER-") for test in result.engineering.fat_tests)
    assert all(test.execution_status == "NOT_RUN" and test.engineer_execution_required for test in result.engineering.fat_tests)
    assert "Validated Schneider EcoStruxure Control Expert" in result.stages[0].summary

    report = render_production_report(result)
    assert "Schneider Control Expert Export Inventory" in report
    assert "simple series ld" in report.lower()
    assert "Control Expert Simulator" in report


def test_schneider_st_control_flow_stays_partial_and_runtime_fat(tmp_path: Path) -> None:
    source = _write(tmp_path / "Sequence.xst", PARTIAL_ST)
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile(result.engineering.project)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["partial_statements"] >= 2
    assert any(item.writes == ("Run",) and item.semantic_state is PLCSemanticState.PARTIAL for item in result.engineering.project.logic_statements)
    assert any(test.method == "RUNTIME_FAT_REQUIRED" for test in result.engineering.fat_tests)
    assert any(risk.category == "SEMANTIC_COVERAGE" for risk in result.risks)


def test_schneider_simple_ld_series_rung_is_bounded_full(tmp_path: Path) -> None:
    source = _write(tmp_path / "Motor.XLD", LD_PROJECT)
    result = run_production_verification_v5(source)
    logic = result.engineering.project.output_logic[0]
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert logic.language == "LD"
    assert logic.instruction == "LD_COIL"
    assert logic.output_tag == "Run"
    assert {term.tag: term.required for term in logic.paths[0].terms} == {"Start": True, "Stop": False}


def test_schneider_fbd_is_imported_but_opaque(tmp_path: Path) -> None:
    source = _write(tmp_path / "Timer.XBD", FBD_PROJECT)
    result = run_production_verification_v5(source)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    statement = result.engineering.project.logic_statements[0]
    assert statement.language == "FBD"
    assert statement.semantic_state is PLCSemanticState.OPAQUE
    assert not result.engineering.project.output_logic
