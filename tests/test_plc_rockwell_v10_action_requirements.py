import json
from pathlib import Path

from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_v5 import run_production_verification_v5


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ActionReq" TargetType="Controller">
  <Controller Name="ActionReq" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Enable)MOV(Source,Dest);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "ActionReq.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _requirements(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "requirements.json"
    path.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-ACTION-001",
                        "text": text,
                        "verification_mode": "STATIC",
                        "criticality": "MEDIUM",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_explicit_move_requirement_gets_local_action_proof_and_fat_link(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _requirements(
        tmp_path,
        "When Enable is TRUE, Dest shall receive Source.",
    )
    result = run_production_verification_v5(project, requirement_paths=[requirements])

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert verification.ai_assisted is False
    assert verification.confidence == 1.0
    assert verification.linked_test_ids
    assert all(test_id.startswith("FAT-ACTION-") for test_id in verification.linked_test_ids)
    assert "local instruction effect only" in verification.summary


def test_vague_move_tag_cooccurrence_is_not_promoted(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _requirements(
        tmp_path,
        "Enable, Source, and Dest shall be reviewed together.",
    )
    result = run_production_verification_v5(project, requirement_paths=[requirements])

    verification = result.requirement_verification[0]
    assert verification.status is not RequirementStatus.ACTION_EFFECT_PROVEN
    assert verification.linked_test_ids == ()
