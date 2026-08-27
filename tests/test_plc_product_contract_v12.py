from __future__ import annotations

import json
from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report


def _project(tmp_path: Path, *, revision: str) -> Path:
    bulk_tags = "".join(
        f'<Tag Name="BulkTag{i:04d}" TagType="Base" DataType="DINT" />'
        for i in range(2500)
    )
    functional = """
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="AltStart" TagType="Base" DataType="BOOL" />
      <Tag Name="Fault" TagType="Base" DataType="BOOL" />
      <Tag Name="EStopOK" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="AltOut" TagType="Base" DataType="BOOL" />
      <Tag Name="ContradictoryOut" TagType="Base" DataType="BOOL" />
      <Tag Name="SafePermit" TagType="Base" DataType="BOOL" />
      <Tag Name="Pulse" TagType="Base" DataType="BOOL" />
      <Tag Name="PulseOut" TagType="Base" DataType="BOOL" />
      <Tag Name="SpareEnable" TagType="Base" DataType="BOOL" />
      <Tag Name="SpareOut" TagType="Base" DataType="BOOL" />
      <Tag Name="Step" TagType="Base" DataType="DINT" />
      <Tag Name="Counter1" TagType="Base" DataType="COUNTER" />
      <Tag Name="Mystery" TagType="Base" DataType="DINT" />
    """
    aux_routines = "".join(
        f'<Routine Name="Aux{i:03d}" Type="RLL"><RLLContent /></Routine>'
        for i in range(200)
    )

    if revision == "baseline":
        rung1 = "XIC(AltStart)OTE(AltOut);"
        rung2 = "XIC(Fault)OTE(ContradictoryOut);"
        rung4 = "EQU(Step,30)XIC(AltStart)MOV(40,Step);"
        rung5 = "XIC(Pulse)OTE(PulseOut);"
    else:
        rung1 = "XIC(AltStart)OTE(Run);"
        rung2 = "XIC(Fault)XIO(Fault)OTE(ContradictoryOut);"
        rung4 = "EQU(Step,10)XIC(AltStart)MOV(30,Step);"
        rung5 = "XIC(Pulse)CTU(Counter1,10,0);"

    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ProductContract" TargetType="Controller">
  <Controller Name="ProductContract" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>{bulk_tags}{functional}</Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Main"><Routines>
        <Routine Name="Main" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
          <Rung Number="1"><Text><![CDATA[{rung1}]]></Text></Rung>
          <Rung Number="2"><Text><![CDATA[{rung2}]]></Text></Rung>
          <Rung Number="3"><Text><![CDATA[EQU(Step,10)XIC(Start)MOV(20,Step);]]></Text></Rung>
          <Rung Number="4"><Text><![CDATA[{rung4}]]></Text></Rung>
          <Rung Number="5"><Text><![CDATA[{rung5}]]></Text></Rung>
          <Rung Number="6"><Text><![CDATA[XIC(EStopOK)OTE(SafePermit);]]></Text></Rung>
          <Rung Number="7"><Text><![CDATA[VendorMystery(Mystery);]]></Text></Rung>
        </RLLContent></Routine>
        {aux_routines}
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
    path = tmp_path / f"ProductContract-{revision}.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _requirements(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.json"
    path.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-RUN",
                        "text": "When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
                        "verification_mode": "STATIC",
                        "criticality": "HIGH",
                    },
                    {
                        "id": "REQ-SAFE",
                        "text": "When EStopOK=TRUE, SafePermit shall be TRUE.",
                        "verification_mode": "STATIC",
                        "criticality": "CRITICAL",
                    },
                    {
                        "id": "REQ-SEQUENCE",
                        "text": "Step sequence shall transition deterministically and shall not have ambiguous next-state ownership.",
                        "verification_mode": "STATIC",
                        "criticality": "HIGH",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_product_contract_covers_full_engineering_review_chain(tmp_path: Path) -> None:
    current = _project(tmp_path, revision="current")
    baseline = _project(tmp_path, revision="baseline")
    requirements = _requirements(tmp_path)

    result = run_production_verification_v5(
        current,
        requirement_paths=[requirements],
        baseline_path=baseline,
    )

    # Read large full-project exports rather than toy single-rung snippets.
    assert len(result.engineering.project.tags) >= 2500
    assert len(result.engineering.project.routines) >= 200

    # Understand/trace machine logic through evidence-linked cause/effect.
    assert result.engineering.graph.edges
    assert any(
        edge.source.casefold() == "run" and edge.target.casefold() in {"start", "guard", "altstart"}
        for edge in result.engineering.graph.edges
    )
    assert any(item.category == "CAUSE_EFFECT" for item in result.engineering_findings)

    # Suspicious logic: multiple writers, unreachable logic, contradiction, sequencing.
    categories = {item.category for item in result.risks}
    assert "MULTIPLE_WRITERS" in categories
    assert "UNREACHABLE_LOGIC" in categories
    assert "CONTRADICTORY_LOGIC" in categories
    assert "SEQUENCING" in categories
    assert any(item.category == "SEQUENCING" for item in result.engineering_findings)

    # Requirements are compared to PLC evidence; unsupported/ambiguous behavior is withheld.
    verification = {item.requirement_id: item for item in result.requirement_verification}
    assert verification["REQ-SAFE"].status is RequirementStatus.STATICALLY_VERIFIED
    assert verification["REQ-RUN"].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert verification["REQ-SEQUENCE"].status in {
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        RequirementStatus.NOT_MAPPED,
        RequirementStatus.AI_CANDIDATE,
    }
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert result.engineering.limitations

    # Determine what MUST be tested and make every recommendation engineer-ready.
    assert any(item.scenario == "STATEFUL_RUNTIME" for item in result.engineering.fat_tests)
    assert any(item.scenario == "STATE_TRANSITION_RUNTIME" for item in result.engineering.fat_tests)
    for test in result.engineering.fat_tests:
        assert test.execution_status == "NOT_RUN"
        assert test.engineer_execution_required is True
        assert test.purpose
        assert test.setup_steps
        assert test.action_steps
        assert test.watch_tags
        assert test.evidence_required
        assert test.why_required
        assert test.failure_implication
        assert test.recommended_environment

    # Re-analysis after PLC change identifies risk/test impact instead of only file diff.
    change_types = {item.change_type for item in result.regression_changes}
    assert "RUNG_SOURCE_CHANGED" in change_types
    assert "RISK_INTRODUCED" in change_types
    assert "FAT_RECOMMENDATION_ADDED" in change_types or "FAT_RECOMMENDATION_CHANGED" in change_types
    assert any("REQ-RUN" in item.affected_requirement_ids for item in result.regression_changes)
    assert any(item.affected_test_ids for item in result.regression_changes)

    report = render_production_report(result)
    assert "## Engineer FAT Procedures" in report
    assert "## Regression Analysis" in report
    assert "DevAgent does not connect to" in report
    assert "**Setup / Preconditions**" in report
    assert "**Test Actions**" in report
    assert "**Evidence to Capture**" in report
    assert "**Failure Implication**" in report
