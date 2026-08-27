from __future__ import annotations

import json
from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report


def _project(tmp_path: Path) -> Path:
    tags = "".join(
        f'<Tag Name="Bulk{i:04d}" TagType="Base" DataType="DINT" />'
        for i in range(500)
    )
    tags += """
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Fault" TagType="Base" DataType="BOOL" />
      <Tag Name="AltStart" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="ConflictOut" TagType="Base" DataType="BOOL" />
      <Tag Name="ContradictoryOut" TagType="Base" DataType="BOOL" />
      <Tag Name="LatchedCmd" TagType="Base" DataType="BOOL" />
      <Tag Name="DupEnable" TagType="Base" DataType="BOOL" />
      <Tag Name="DupOut" TagType="Base" DataType="BOOL" />
      <Tag Name="SpareEnable" TagType="Base" DataType="BOOL" />
      <Tag Name="SpareOut" TagType="Base" DataType="BOOL" />
      <Tag Name="Step" TagType="Base" DataType="DINT" />
    """
    aux = "".join(
        f'<Routine Name="Aux{i:03d}" Type="RLL"><RLLContent /></Routine>'
        for i in range(40)
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="FourContract" TargetType="Controller">
  <Controller Name="FourContract" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Main"><Routines>
        <Routine Name="Main" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
          <Rung Number="1"><Text><![CDATA[XIC(AltStart)OTE(Run);]]></Text></Rung>
          <Rung Number="2"><Text><![CDATA[XIC(Start)OTE(ConflictOut);]]></Text></Rung>
          <Rung Number="3"><Text><![CDATA[XIC(Fault)XIO(Fault)OTE(ContradictoryOut);]]></Text></Rung>
          <Rung Number="4"><Text><![CDATA[XIC(Start)OTL(LatchedCmd);]]></Text></Rung>
          <Rung Number="5"><Text><![CDATA[EQU(Step,10)XIC(Start)MOV(20,Step);]]></Text></Rung>
          <Rung Number="6"><Text><![CDATA[EQU(Step,10)XIC(AltStart)MOV(30,Step);]]></Text></Rung>
          <Rung Number="7"><Text><![CDATA[XIC(DupEnable)OTE(DupOut);]]></Text></Rung>
          <Rung Number="8"><Text><![CDATA[XIC(DupEnable)OTE(DupOut);]]></Text></Rung>
        </RLLContent></Routine>
        {aux}
      </Routines></Program>
      <Program Name="SpareProgram" MainRoutineName="Spare"><Routines>
        <Routine Name="Spare" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(SpareEnable)OTE(SpareOut);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "FourContract.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _requirements(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.json"
    path.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-CONFLICT",
                        "text": "When Start=TRUE, ConflictOut shall be FALSE.",
                        "verification_mode": "STATIC",
                        "criticality": "HIGH",
                    },
                    {
                        "id": "REQ-RUN",
                        "text": "When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
                        "verification_mode": "STATIC",
                        "criticality": "HIGH",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_four_core_product_contract_is_complete_and_evidence_backed(tmp_path: Path) -> None:
    result = run_production_verification_v5(
        _project(tmp_path),
        requirement_paths=[_requirements(tmp_path)],
    )

    # 1. ENGINEERING ANALYSIS: machine logic, cause/effect, sequencing, dependencies.
    project = result.engineering.project
    finding_categories = {item.category for item in result.engineering_findings}
    assert len(project.tags) >= 500
    assert project.output_logic
    assert result.engineering.graph.edges
    assert "PROJECT_INVENTORY" in finding_categories
    assert "CAUSE_EFFECT" in finding_categories
    assert "SEQUENCING" in finding_categories
    assert any(edge.kind == "STATE_TRANSITION" for edge in result.engineering.graph.edges)

    # 2. RISKS / LOGIC PROBLEMS: deterministic defects and suspicious behavior.
    risk_categories = {item.category for item in result.risks}
    assert "MULTIPLE_WRITERS" in risk_categories
    assert "CONTRADICTORY_LOGIC" in risk_categories
    assert "UNREACHABLE_LOGIC" in risk_categories
    assert "SEQUENCING" in risk_categories
    assert "RETENTIVE_LOGIC" in risk_categories
    verification = {item.requirement_id: item for item in result.requirement_verification}
    assert verification["REQ-CONFLICT"].status is RequirementStatus.CONFLICT

    # 3. OPTIMIZATION RECOMMENDATIONS: all five commercial areas are implemented.
    optimization_categories = {item.category for item in result.optimizations}
    assert "MAINTAINABILITY" in optimization_categories
    assert "SIMPLIFICATION" in optimization_categories
    assert "DUPLICATION" in optimization_categories
    assert "OWNERSHIP" in optimization_categories
    assert "STRUCTURAL_IMPROVEMENT" in optimization_categories
    for item in result.optimizations:
        assert item.current_state
        assert item.proposed_change
        assert item.expected_benefit
        assert item.evidence_ids

    # 4. FAT PLAN: every generated case is an engineer-ready manual procedure.
    assert result.engineering.fat_tests
    for test in result.engineering.fat_tests:
        assert test.execution_status == "NOT_RUN"
        assert test.engineer_execution_required is True
        assert test.why_required
        assert test.setup_steps
        assert test.action_steps
        assert test.expected
        assert test.watch_tags
        assert test.evidence_required
        assert test.failure_implication

    report = render_production_report(result)
    assert "## DevAgent Four-Core PLC Review Contract" in report
    assert "### 1. Engineering Analysis" in report
    assert "### 2. Risks / Logic Problems" in report
    assert "### 3. Optimization Recommendations" in report
    assert "### 4. FAT Plan" in report
    assert "## Engineer FAT Procedures" in report
    assert "**Setup / Preconditions**" in report
    assert "**Test Actions**" in report
    assert "**Expected Result**" in report
    assert "DevAgent does not connect to" in report

    # 5. PROFESSIONAL REPORT: executive/customer layer plus full engineering detail.
    assert "## Executive Summary" in report
    assert "### Document Control" in report
    assert "### Management Dashboard" in report
    assert "### Attention Summary" in report
    assert "### Highest-Priority Logic / Risk Findings" in report
    assert "### Priority Engineering Actions" in report
    assert "### FAT Readiness Snapshot" in report
    assert "### Report Navigation" in report
    assert "## Technical Verification Identity" in report
    assert "## Requirement Verification" in report
    assert "## Risk Detection" in report
    assert "## Optimization Review" in report
    assert "## Regression Analysis" in report
    assert "## Recommendations" in report
    assert "## Release Readiness" in report
    assert "## Verification Boundaries" in report
    assert report.index("## Executive Summary") < report.index("## Technical Verification Identity")
    assert report.index("## Technical Verification Identity") < report.index("## 15-Stage Pipeline")
