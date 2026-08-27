from __future__ import annotations

import hashlib
from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.providers import ScriptedFakeProvider


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="HarnessTrace" TargetType="Controller">
  <Controller Name="HarnessTrace" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Main" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def test_v15_production_run_persists_bounded_agent_graph_trace(tmp_path: Path) -> None:
    project = tmp_path / "HarnessTrace.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    project_sha = hashlib.sha256(project.read_bytes()).hexdigest()
    evidence_id = f"ROCKWELL-CAPABILITY:{project_sha}"
    provider = ScriptedFakeProvider(
        [
            {
                "_role": "plc_engineering_reviewer",
                "findings": [
                    {
                        "id": "TRACE-1",
                        "category": "ENGINEERING_REVIEW",
                        "title": "Review Rockwell support boundary",
                        "severity": "LOW",
                        "summary": "Review the bounded Rockwell capability profile; unsupported or partial semantics remain outside proof.",
                        "recommendation": "Keep unsupported or partial behavior fail-closed and route it to engineer review/FAT where appropriate.",
                        "evidence_ids": [evidence_id],
                        "confidence": 0.7,
                    }
                ],
            },
            {
                "_role": "plc_engineering_critic",
                "decisions": [
                    {
                        "finding_id": "TRACE-1",
                        "decision": "ACCEPT",
                        "reason": "The finding remains bounded to the supplied Rockwell capability evidence and makes no runtime claim.",
                        "supported_evidence_ids": [evidence_id],
                    }
                ],
            },
        ]
    )

    result = run_production_verification_v5(
        project,
        provider=provider,
        ai_enabled=True,
        ai_provider_name="fake",
        ai_model_name="fake",
    )

    assert result.ai_harness_trace
    assert result.ai_harness_trace[0]["graph"] == "PLC_ENGINEERING_REVIEW"
    assert result.ai_harness_trace[0]["node"] == "CONTEXT"
    assert result.ai_harness_trace[-1]["node"] == "ACCEPT"
    assert any(item.id == "AI-TRACE-1" for item in result.engineering_findings)
