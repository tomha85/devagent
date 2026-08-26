from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_evidence import evidence_index
from devagent.plc.production_models import (
    ExecutionStatus,
    PLCRequirement,
    RequirementCriticality,
    RequirementStatus,
    RequirementVerificationMode,
    TestExecutionEvidence as ExecutionEvidence,
)
from devagent.plc import production_verification
from devagent.plc.rockwell_semantic_capabilities import (
    RockwellSemanticKind,
    instruction_capability,
)


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="GeneralSemantics" TargetType="Controller">
  <Controller Use="Target" Name="GeneralSemantics" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="SetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="ResetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="Latched" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(SetRequest)OTL(Latched);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(ResetRequest)OTU(Latched);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "GeneralSemantics.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_project_with_condition_writer(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ConditionWriter" TargetType="Controller">
  <Controller Use="Target" Name="ConditionWriter" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="SetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="ResetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="Latched" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(SetRequest)OTE(ResetRequest);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(SetRequest)OTL(Latched);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[XIC(ResetRequest)OTU(Latched);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "ConditionWriter.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _write_project_with_subroutine_writer(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="CrossRoutine" TargetType="Controller">
  <Controller Use="Target" Name="CrossRoutine" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="SetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="ResetRequest" TagType="Base" DataType="BOOL" />
      <Tag Name="Latched" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(SetRequest)OTL(Latched);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[JSR(SubRoutine,0);]]></Text></Rung>
      </RLLContent></Routine>
      <Routine Name="SubRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(ResetRequest)OTU(Latched);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "CrossRoutine.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _requirement(req_id: str, text: str) -> PLCRequirement:
    return PLCRequirement(
        req_id,
        text,
        "requirements.json",
        "item 1",
        "f" * 64,
        RequirementVerificationMode.STATIC,
        RequirementCriticality.MEDIUM,
    )


def _verify(path: Path, requirement: PLCRequirement):
    engineering = analyze_rockwell_l5x(path)
    result = production_verification.verify_requirement(
        requirement,
        engineering,
        evidence_index(engineering),
        engineering.fat_tests,
    )
    return engineering, result


def test_v10_capability_registry_distinguishes_continuous_and_retentive_outputs() -> None:
    ote = instruction_capability("OTE")
    otl = instruction_capability("otl")
    otu = instruction_capability("OTU")

    assert ote is not None and ote.final_state_provable_from_path_only is True
    assert otl is not None and otl.semantic_kind is RockwellSemanticKind.RETENTIVE_ACTION
    assert otl.fixed_action_value is True
    assert otl.final_state_provable_from_path_only is False
    assert otu is not None and otu.semantic_kind is RockwellSemanticKind.RETENTIVE_ACTION
    assert otu.fixed_action_value is False
    assert otu.final_state_provable_from_path_only is False


def test_otl_requirement_proves_local_action_without_false_final_state_claim(tmp_path: Path) -> None:
    engineering, result = _verify(
        _write_project(tmp_path),
        _requirement("REQ-LATCH", "IF SetRequest=TRUE THEN Latched=TRUE"),
    )

    assert result.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert "local instruction action only" in result.summary
    assert "final scan state remains NOT_PROVEN" in result.summary
    assert result.linked_test_ids
    assert any(test.id in result.linked_test_ids for test in engineering.fat_tests)


def test_otu_requirement_proves_local_action_and_links_existing_fat(tmp_path: Path) -> None:
    engineering, result = _verify(
        _write_project(tmp_path),
        _requirement("REQ-UNLATCH", "IF ResetRequest=TRUE THEN Latched=FALSE"),
    )

    assert result.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert "OTU" in result.summary
    assert result.linked_test_ids
    linked = [test for test in engineering.fat_tests if test.id in result.linked_test_ids]
    assert linked
    assert all(test.output_tag == "Latched" for test in linked)
    assert any(test.preconditions.get("ResetRequest") is True for test in linked)


def test_action_effect_does_not_claim_opposite_retained_state(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project(tmp_path),
        _requirement("REQ-OPPOSITE", "IF ResetRequest=TRUE THEN Latched=TRUE"),
    )

    assert result.status is RequirementStatus.TRACEABLE_NOT_PROVEN


def test_same_main_routine_order_can_prove_final_retained_state(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project(tmp_path),
        _requirement(
            "REQ-SCAN-FINAL",
            "IF SetRequest=TRUE AND ResetRequest=FALSE THEN Latched=TRUE",
        ),
    )

    assert result.status is RequirementStatus.STATICALLY_VERIFIED
    assert "same-routine Rockwell scan ordering" in result.summary
    assert "final Latched=TRUE" in result.summary
    assert result.ai_assisted is False


def test_later_same_routine_otu_can_prove_conflict(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project(tmp_path),
        _requirement(
            "REQ-SCAN-CONFLICT",
            "IF SetRequest=TRUE AND ResetRequest=TRUE THEN Latched=TRUE",
        ),
    )

    assert result.status is RequirementStatus.CONFLICT
    assert "final Latched=FALSE" in result.summary


def test_scan_order_withholds_when_condition_is_rewritten_in_scan(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project_with_condition_writer(tmp_path),
        _requirement(
            "REQ-UNSTABLE-CONDITION",
            "IF SetRequest=TRUE AND ResetRequest=FALSE THEN Latched=TRUE",
        ),
    )

    assert result.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert "final scan state remains NOT_PROVEN" in result.summary


def test_scan_order_withholds_across_subroutine_boundary(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project_with_subroutine_writer(tmp_path),
        _requirement(
            "REQ-CROSS-ROUTINE",
            "IF SetRequest=TRUE AND ResetRequest=FALSE THEN Latched=TRUE",
        ),
    )

    assert result.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert "final scan state remains NOT_PROVEN" in result.summary


def test_qualified_execution_can_promote_action_effect_to_dynamic_verification(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project(tmp_path),
        _requirement("REQ-DYNAMIC", "IF ResetRequest=TRUE THEN Latched=FALSE"),
    )
    assert result.status is RequirementStatus.ACTION_EFFECT_PROVEN
    assert result.linked_test_ids

    executions = [
        ExecutionEvidence(
            test_id=test_id,
            status=ExecutionStatus.PASS,
            backend="qualified-echo",
            run_id="run-1",
        )
        for test_id in result.linked_test_ids
    ]
    promoted = production_verification.promote_requirement_execution([result], executions)

    assert promoted[0].status is RequirementStatus.DYNAMICALLY_VERIFIED


def test_failed_qualified_execution_turns_action_effect_into_conflict(tmp_path: Path) -> None:
    _, result = _verify(
        _write_project(tmp_path),
        _requirement("REQ-DYNAMIC-FAIL", "IF ResetRequest=TRUE THEN Latched=FALSE"),
    )
    assert result.linked_test_ids

    executions = [
        ExecutionEvidence(
            test_id=result.linked_test_ids[0],
            status=ExecutionStatus.FAIL,
            backend="qualified-echo",
            run_id="run-2",
        )
    ]
    promoted = production_verification.promote_requirement_execution([result], executions)

    assert promoted[0].status is RequirementStatus.CONFLICT
