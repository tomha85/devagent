from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_report import render_production_report
from devagent.plc.rockwell_system_service_v17 import system_service_profile


def _project(tmp_path: Path, *, include_unrelated_unknown: bool = False) -> Path:
    unknown = (
        '<Rung Number="2"><Text><![CDATA[VendorMystery(Source,Dest);]]></Text></Rung>'
        if include_unrelated_unknown
        else ""
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="SystemServiceCoverage" TargetType="Controller">
  <Controller Name="SystemServiceCoverage" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Record" TagType="Base" DataType="DINT" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="Faulted" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Main"><Routines>
        <Routine Name="Main" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GSV(Program,THIS,MajorFaultRecord,Record)[MOVE(Source,Dest),CLR(Record),SSV(Program,THIS,MajorFaultRecord,Record),OTE(Faulted)];]]></Text></Rung>
          <Rung Number="1"><Text><![CDATA[XIC(Faulted)MOVE(Source,Dest);]]></Text></Rung>
          {unknown}
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / ("SystemServiceUnknown.L5X" if include_unrelated_unknown else "SystemServiceCoverage.L5X")
    path.write_text(payload, encoding="utf-8")
    return path


def _system_test(result):
    matches = [
        test
        for test in result.engineering.fat_tests
        if test.scenario == "SYSTEM_SERVICE_RUNTIME"
    ]
    assert len(matches) == 1
    return matches[0]


def test_v17_major_fault_system_service_generates_manual_runtime_fat_without_proof_promotion(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    project = result.engineering.project
    profile = system_service_profile(project)
    test = _system_test(result)

    assert profile["occurrences"] == 2
    assert profile["rungs"] == 1
    assert profile["gsv_occurrences"] == 1
    assert profile["ssv_occurrences"] == 1
    assert profile["major_fault_record_occurrences"] == 2
    assert profile["runtime_fat_tests"] == 1
    assert profile["requires_engineer_runtime_evidence"] is True

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert project.branch_rung_total == 1
    assert project.branch_rung_semantic_count == 0
    assert {name.upper() for name in project.partially_modeled_instruction_names} == {"GSV", "SSV"}

    assert test.method == "RUNTIME_FAT_REQUIRED"
    assert test.execution_status == "NOT_RUN"
    assert test.engineer_execution_required is True
    assert "MajorFaultRecord" in test.expected
    assert "runtime evidence" in test.expected
    assert "isolated simulator" in test.recommended_environment
    assert "not an operating production machine" in test.recommended_environment
    assert any("controlled" in step.lower() and "fault" in step.lower() for step in test.setup_steps)
    assert any("diagnostic" in step.lower() for step in test.action_steps)
    assert any("timestamped" in item.lower() for item in test.evidence_required)
    assert all("DevAgent does not" in item or "PARTIAL" in item or "runtime" in item.lower() for item in test.limitations)

    service_check = next(
        check
        for check in result.engineering.static_checks
        if check.id == "ROCKWELL_SYSTEM_SERVICE_RUNTIME"
    )
    assert service_check.status.value == "WARN"
    assert "engineer-executed runtime FAT" in service_check.summary

    traceability = next(
        check
        for check in result.engineering.static_checks
        if check.id == "FAT_TEST_TRACEABILITY"
    )
    assert traceability.status.value == "PASS"
    assert "system-service runtime procedure" in traceability.summary

    assert result.readiness is not None
    assert result.readiness.status.value == "NOT_READY"


def test_v17_replaces_only_vague_semantic_risk_when_system_service_is_entire_gap(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    categories = [risk.category for risk in result.risks]
    service = [risk for risk in result.risks if risk.category == "SYSTEM_SERVICE_RUNTIME"]
    test = _system_test(result)

    assert categories.count("SYSTEM_SERVICE_RUNTIME") == 1
    assert "SEMANTIC_COVERAGE" not in categories
    assert len(service) == 1
    assert service[0].severity.value == "HIGH"
    assert "Major-fault" in service[0].title
    assert test.id in service[0].evidence_ids


def test_v17_keeps_generic_semantic_risk_when_an_unrelated_gap_also_exists(tmp_path: Path) -> None:
    result = run_production_verification_v5(
        _project(tmp_path, include_unrelated_unknown=True)
    )
    categories = [risk.category for risk in result.risks]

    assert "SYSTEM_SERVICE_RUNTIME" in categories
    assert "SEMANTIC_COVERAGE" in categories
    assert "VENDORMYSTERY" in {name.upper() for name in result.engineering.project.unknown_instruction_names}
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v17_report_explains_runtime_boundary_and_preserves_project_only_contract(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    report = render_production_report(result)
    test = _system_test(result)

    assert "### Rockwell System-Service Runtime Boundary" in report
    assert "Reachable GSV/SSV occurrences: **2** across **1** rung(s)" in report
    assert "MajorFaultRecord occurrences: **2**" in report
    assert "System-service runtime FAT procedures: **1**" in report
    assert "Static proof promotion: **none**" in report
    assert f"### {test.id} —" in report
    assert "SYSTEM_SERVICE_RUNTIME" in report
    assert "controlled fault stimulus" in report
    assert "DevAgent execution status: **NOT_RUN**" in report

    assert "**Review mode:** **PROJECT_ONLY_ENGINEERING_REVIEW**" in report
    assert "**Requirement compliance:** **NOT EVALUATED" in report
    assert "**Engineering outcome:** **PARTIALLY_VERIFIED**" in report
    assert "**Release readiness:** **NOT_READY**" in report
