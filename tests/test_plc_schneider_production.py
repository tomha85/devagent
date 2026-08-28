from __future__ import annotations

from pathlib import Path

import pytest

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.plc_dispatch import detect_plc_vendor
from devagent.plc.production_models import RequirementStatus
from devagent.plc.schneider_control_expert_v1 import SchneiderInputError


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

VARIABLES = """<?xml version="1.0" encoding="UTF-8"?>
<VariablesExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert" DTDVersion="41" />
  <contentHeader name="Vars" version="1.0" />
  <dataBlock>
    <variables name="Start" typeName="BOOL" topologicalAddress="%I0.0" />
    <variables name="Run" typeName="BOOL" topologicalAddress="%Q0.0" />
  </dataBlock>
</VariablesExchangeFile>
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_schneider_directory_combines_variables_and_logic(tmp_path: Path) -> None:
    export = tmp_path / "ControlExpertExport"
    export.mkdir()
    _write(export / "Variables.XSY", VARIABLES)
    _write(export / "Main.XST", ST_PROJECT)
    assert detect_plc_vendor(export) == "SCHNEIDER"
    result = analyze_plc_project(export)
    assert result.project.metadata.vendor == "Schneider Electric"
    assert {tag.name for tag in result.project.tags} >= {"Start", "Stop", "Run"}
    assert any("%I0.0" in (tag.description or "") for tag in result.project.tags if tag.name == "Start")


def test_schneider_boolean_requirement_can_be_proven_without_runtime_claim(tmp_path: Path) -> None:
    source = _write(tmp_path / "Main.xst", ST_PROJECT)
    requirements = _write(tmp_path / "requirements.md", "REQ-001: When Start=TRUE and Stop=FALSE, Run=TRUE.\n")
    result = run_production_verification_v5(source, requirement_paths=[requirements])
    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert verification.linked_test_ids
    assert result.executions == []
    assert all(test.execution_status == "NOT_RUN" for test in result.engineering.fat_tests)


def test_schneider_boolean_requirement_conflict_is_deterministic(tmp_path: Path) -> None:
    source = _write(tmp_path / "Main.xst", ST_PROJECT)
    requirements = _write(tmp_path / "requirements.md", "REQ-002: When Start=TRUE and Stop=FALSE, Run=FALSE.\n")
    result = run_production_verification_v5(source, requirement_paths=[requirements])
    assert result.requirement_verification[0].status is RequirementStatus.CONFLICT
    assert any(risk.category == "REQUIREMENT" and risk.severity.value == "CRITICAL" for risk in result.risks)


def test_schneider_multiple_writers_create_ownership_risk(tmp_path: Path) -> None:
    source_text = ST_PROJECT.replace(
        "Run := Start AND NOT Stop;",
        "Run := Start AND NOT Stop;\nRun := Guard AND NOT Stop;",
    ).replace(
        '<variables name="Run" typeName="BOOL" />',
        '<variables name="Run" typeName="BOOL" />\n    <variables name="Guard" typeName="BOOL" />',
    )
    source = _write(tmp_path / "Writers.xst", source_text)
    result = run_production_verification_v5(source)
    writer_risks = [risk for risk in result.risks if risk.category == "MULTIPLE_WRITERS"]
    assert len(writer_risks) == 1
    assert "Run" in writer_risks[0].title
    # Current V8/V9 writer-ownership hardening deliberately fails closed: the
    # local Boolean statements remain traceable, but competing writers prevent a
    # project-wide full static outcome.
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_schneider_work_archive_formats_fail_closed(tmp_path: Path) -> None:
    for suffix in (".stu", ".sta", ".zef"):
        path = _write(tmp_path / f"Project{suffix}", "placeholder")
        with pytest.raises(SchneiderInputError):
            detect_plc_vendor(path)


def test_schneider_preflight_rejects_oversize_before_xml_parse(tmp_path: Path) -> None:
    path = tmp_path / "Huge.xef"
    with path.open("wb") as handle:
        handle.seek(100 * 1024 * 1024)
        handle.write(b"x")
    with pytest.raises(SchneiderInputError, match="100 MiB"):
        analyze_plc_project(path)


def test_mixed_siemens_and_schneider_directory_fails_closed(tmp_path: Path) -> None:
    export = tmp_path / "mixed"
    export.mkdir()
    _write(export / "Main.xst", ST_PROJECT)
    _write(
        export / "OB1.scl",
        'ORGANIZATION_BLOCK "OB1"\nVAR_TEMP\n Start : Bool;\n Run : Bool;\nEND_VAR\nBEGIN\n Run := Start;\nEND_ORGANIZATION_BLOCK\n',
    )
    with pytest.raises(ValueError, match="ambiguous"):
        detect_plc_vendor(export)
