from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.siemens_call_graph_v3 import siemens_capability_profile_v3


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _req(path: Path, text: str) -> Path:
    return _write(path, text)


def _direct_fc(path: Path, *, actual: str = "MainStart") -> Path:
    return _write(
        path,
        f'''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    Guard : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := {actual}, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )


def test_v3_direct_fc_call_projects_reachable_boolean_theorem_and_requirement(
    tmp_path: Path,
) -> None:
    source = _direct_fc(tmp_path / "DirectFC.scl")
    requirement = _req(
        tmp_path / "requirements.md",
        "REQ-CALL-001: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    project = result.engineering.project
    facts = project._siemens_v3_facts
    profile = siemens_capability_profile_v3(project)

    assert project.metadata.schema_revision == "SIEMENS-TIA-EXPORT-V3"
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["schema"] == "devagent-siemens-tia-capability-v3"
    assert profile["execution_closure"] == "COMPLETE"
    assert profile["calls"] == 1
    assert profile["calls_bound"] == 1
    assert set(profile["reachable_blocks"]) == {"Main", "LogicFC"}
    assert not profile["unreachable_blocks"]

    call = facts.calls[0]
    assert call.caller_block == "Main"
    assert call.callee_block == "LogicFC"
    assert call.instance_db is None
    assert call.semantic_state is PLCSemanticState.FULL
    assert call.resolution == "direct_fc"
    assert {
        (item.formal, item.actual, item.direction)
        for item in call.bindings
    } == {
        ("Start", "MainStart", "VAR_INPUT"),
        ("Run", "MotorRun", "VAR_OUTPUT"),
    }

    projected = [
        item
        for item in project.output_logic
        if item.id in set(facts.projected_logic_ids)
        and item.output_tag == "MotorRun"
    ]
    assert len(projected) == 1
    assert {
        (term.tag, term.required)
        for term in projected[0].paths[0].terms
    } == {("MainStart", True)}
    assert any(
        edge.kind == "CALLS_BLOCK"
        and edge.source == "BLOCK:Main"
        and edge.target == "BLOCK:LogicFC"
        for edge in result.engineering.graph.edges
    )
    assert any(
        edge.kind == "BINDS_INPUT" and edge.target == "MainStart"
        for edge in result.engineering.graph.edges
    )
    assert any(
        edge.kind == "BINDS_OUTPUT" and edge.target == "MotorRun"
        for edge in result.engineering.graph.edges
    )

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert "Siemens V3 additionally proves" in verification.summary
    assert call.id in verification.evidence_ids
    assert verification.linked_test_ids

    report = render_production_report(result)
    assert "### Siemens V3 Call/Interface Execution Closure" in report
    assert (
        "Requirement PASS across FB/FC boundaries requires proven target identity"
        in report
    )


def test_v3_unreachable_fc_local_theorem_cannot_prove_active_requirement(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "Unreachable.scl",
        '''
FUNCTION "DeadFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Heartbeat : Bool;
END_VAR
BEGIN
    Heartbeat := TRUE;
END_ORGANIZATION_BLOCK
''',
    )
    requirement = _req(
        tmp_path / "requirements.md",
        "REQ-CALL-002: When Start=TRUE, Run=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v3_facts

    assert facts.unreachable_blocks == ("DeadFC",)
    assert (
        result.requirement_verification[0].status
        is RequirementStatus.TRACEABLE_NOT_PROVEN
    )
    assert (
        "local Siemens FB/FC theorem"
        in result.requirement_verification[0].summary
    )
    assert any(risk.category == "UNREACHABLE_LOGIC" for risk in result.risks)
    check = next(
        item
        for item in result.engineering.static_checks
        if item.id == "SIEMENS_V3_UNREACHABLE_BLOCKS"
    )
    assert check.status.value == "WARN"


def test_v3_fb_global_instance_db_binding_is_proven(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "InstanceDB.scl",
        '''
FUNCTION_BLOCK "MotorFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION_BLOCK

DATA_BLOCK "MotorDB" "MotorFB"
BEGIN
END_DATA_BLOCK

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    MotorDB(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v3_facts

    assert [
        (item.name, item.block_type) for item in facts.instance_dbs
    ] == [("MotorDB", "MotorFB")]
    assert len(facts.calls) == 1
    assert facts.calls[0].callee_block == "MotorFB"
    assert facts.calls[0].instance_db == "MotorDB"
    assert facts.calls[0].resolution == "instance_db"
    assert facts.calls[0].semantic_state is PLCSemanticState.FULL
    assert set(facts.reachable_blocks) == {"Main", "MotorFB"}
    assert any(
        edge.kind == "USES_INSTANCE_DB"
        and edge.target == "INSTANCE_DB:MotorDB"
        for edge in result.engineering.graph.edges
    )


def test_v3_var_stat_multi_instance_closes_nested_ob_fb_fb_path(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "NestedCalls.scl",
        '''
FUNCTION_BLOCK "ChildFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION_BLOCK

FUNCTION_BLOCK "ParentFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
VAR_STAT
    Child : ChildFB;
END_VAR
BEGIN
    Child(Start := Start, Run => Run);
END_FUNCTION_BLOCK

DATA_BLOCK "ParentDB" "ParentFB"
BEGIN
END_DATA_BLOCK

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    ParentDB(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    requirement = _req(
        tmp_path / "requirements.md",
        "REQ-CALL-003: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v3_facts

    assert len(facts.calls) == 2
    assert not facts.active_call_gaps
    assert set(facts.reachable_blocks) == {"Main", "ParentFB", "ChildFB"}
    child_call = next(
        item for item in facts.calls if item.caller_block == "ParentFB"
    )
    assert child_call.callee_block == "ChildFB"
    assert child_call.instance_db == "ParentFB.Child"
    assert child_call.resolution == "multi_instance"

    projected = [
        item
        for item in result.engineering.project.output_logic
        if item.id in set(facts.projected_logic_ids)
        and item.output_tag == "MotorRun"
    ]
    assert projected
    assert any(
        {
            (term.tag, term.required)
            for term in path.terms
        } == {("MainStart", True)}
        for logic in projected
        for path in logic.paths
    )
    assert (
        result.requirement_verification[0].status
        is RequirementStatus.STATICALLY_VERIFIED
    )


def test_v3_missing_required_input_fails_closed_and_generates_runtime_fat(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "MissingInput.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
    Guard : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start AND Guard;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v3_facts
    call = facts.calls[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert call.semantic_state is PLCSemanticState.PARTIAL
    assert call.resolution.startswith("missing_required_binding:")
    assert call.id in facts.active_call_gaps
    assert any(
        item.scenario == "SIEMENS_CALL_RUNTIME"
        and item.method == "RUNTIME_FAT_REQUIRED"
        for item in result.engineering.fat_tests
    )
    assert any(risk.category == "CALL_BINDING" for risk in result.risks)


def test_v3_unknown_or_complex_actual_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "ComplexActual.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := MainStart AND TRUE, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    call = result.engineering.project._siemens_v3_facts.calls[0]

    assert call.semantic_state is PLCSemanticState.PARTIAL
    assert call.resolution == "complex_or_unknown_actual:Start"
    assert not result.engineering.project._siemens_v3_facts.projected_logic_ids


def test_v3_call_inside_if_remains_partial(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "GuardedCall.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Enable : Bool;
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    IF Enable THEN
        LogicFC(Start := MainStart, Run => MotorRun);
    ELSE
        MotorRun := FALSE;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v3_facts
    call = facts.calls[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert call.semantic_state is PLCSemanticState.PARTIAL
    assert call.resolution == "call_inside_unmodeled_control"
    assert call.id in facts.active_call_gaps
    assert not facts.projected_logic_ids


def test_v3_recursive_call_cycle_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Recursive.scl",
        '''
FUNCTION "A"
BEGIN
    B();
END_FUNCTION

FUNCTION "B"
BEGIN
    A();
END_FUNCTION

ORGANIZATION_BLOCK "Main"
BEGIN
    A();
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v3_facts

    assert set(facts.recursive_blocks) == {"A", "B"}
    assert set(facts.reachable_blocks) == {"Main", "A", "B"}
    assert any(
        item.resolution == "recursive_call_cycle_unsupported"
        for item in facts.calls
    )
    assert any(risk.category == "CALL_RECURSION" for risk in result.risks)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v3_ambiguous_call_target_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Ambiguous.scl",
        '''
FUNCTION "Thing"
BEGIN
END_FUNCTION

FUNCTION_BLOCK "WorkerFB"
BEGIN
END_FUNCTION_BLOCK

DATA_BLOCK "Thing" "WorkerFB"
BEGIN
END_DATA_BLOCK

ORGANIZATION_BLOCK "Main"
BEGIN
    Thing();
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    call = result.engineering.project._siemens_v3_facts.calls[0]

    assert call.semantic_state is PLCSemanticState.PARTIAL
    assert call.resolution == "ambiguous_or_unresolved_target"
    assert any(risk.category == "CALL_BINDING" for risk in result.risks)


def test_v3_competing_direct_and_call_writer_blocks_projection(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "WriterConflict.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    Override : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := MainStart, Run => MotorRun);
    MotorRun := Override;
END_ORGANIZATION_BLOCK
''',
    )
    requirement = _req(
        tmp_path / "requirements.md",
        "REQ-CALL-004: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    facts = result.engineering.project._siemens_v3_facts

    assert facts.writer_conflicts
    assert facts.calls[0].semantic_state is PLCSemanticState.PARTIAL
    assert facts.calls[0].resolution == "competing_output_writer"
    assert not any(
        item.id in set(facts.projected_logic_ids)
        and item.output_tag == "MotorRun"
        for item in result.engineering.project.output_logic
    )
    assert (
        result.requirement_verification[0].status
        is RequirementStatus.TRACEABLE_NOT_PROVEN
    )
    assert any(
        risk.category == "MULTIPLE_WRITERS"
        and "Competing reachable Siemens writers" in risk.title
        for risk in result.risks
    )


def test_v3_call_binding_change_drives_requirement_and_fat_regression(
    tmp_path: Path,
) -> None:
    baseline = _direct_fc(
        tmp_path / "baseline.scl",
        actual="MainStart",
    )
    current = _direct_fc(
        tmp_path / "current.scl",
        actual="Guard",
    )
    requirement = _req(
        tmp_path / "requirements.md",
        "REQ-CALL-005: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        current,
        requirement_paths=[requirement],
        baseline_path=baseline,
    )

    assert (
        result.requirement_verification[0].status
        is RequirementStatus.TRACEABLE_NOT_PROVEN
    )
    relevant = [
        item
        for item in result.regression_changes
        if "MotorRun" in item.subject or "MotorRun" in item.affected_tags
    ]
    assert relevant
    assert any(
        "REQ-CALL-005" in item.affected_requirement_ids
        for item in relevant
    )
    assert any(item.affected_test_ids for item in relevant)


def test_v3_evidence_exposes_block_instance_call_and_support_contract(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "Evidence.scl",
        '''
FUNCTION_BLOCK "MotorFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION_BLOCK

DATA_BLOCK "MotorDB" "MotorFB"
BEGIN
END_DATA_BLOCK

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    MotorDB(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)

    kinds = {item.kind for item in result.evidence}
    assert "SIEMENS_BLOCK" in kinds
    assert "SIEMENS_INSTANCE_DB" in kinds
    assert "SIEMENS_CALL_BINDING" in kinds
    check_ids = {item.id for item in result.engineering.static_checks}
    assert {
        "SIEMENS_V3_CALL_BINDING",
        "SIEMENS_V3_EXECUTION_CLOSURE",
        "SIEMENS_V3_UNREACHABLE_BLOCKS",
        "SIEMENS_V3_CROSS_BLOCK_WRITERS",
    } <= check_ids
