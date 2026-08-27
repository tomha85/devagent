from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_interlock_permissive_v6 import siemens_capability_profile_v6
from devagent.plc.siemens_recovery_v7 import siemens_capability_profile_v7


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _numeric_source(body: str, declarations: str = "") -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : Int;
    Start : Bool;
    DoorInterlock : Bool;
    MotorReady : Bool;
    ResetCmd : Bool;
    Done : Bool;
{declarations}
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def _named_source(body: str) -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : MachineState;
    TripDetected : Bool;
    ResetCmd : Bool;
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def test_v6_classifies_source_guards_without_guessing_and_generates_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "guards.scl",
        _numeric_source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock AND MotorReady THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )

    result = analyze_plc_project(source)
    project = result.project
    profile = siemens_capability_profile_v6(project)
    facts = project._siemens_v6_guard_facts

    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert project.metadata.schema_revision == "SIEMENS-TIA-EXPORT-V7"
    assert profile["schema"] == "devagent-siemens-tia-capability-v6"
    assert profile["guard_contract"] == "COMPLETE"
    assert profile["transition_guard_contracts"] == 2
    assert profile["classified_interlock_terms"] == 1
    assert profile["classified_permissive_terms"] == 1
    assert profile["classified_recovery_terms"] == 1
    assert profile["unclassified_guard_terms"] == 1

    first = next(
        contract
        for contract in facts.contracts
        if contract.source_state == "0" and contract.target_state == "10"
    )
    roles = {term.tag: term.role for term in first.terms}
    assert roles["Start"] == "GUARD"
    assert roles["DoorInterlock"] == "INTERLOCK"
    assert roles["MotorReady"] == "PERMISSIVE"

    assert any(
        test.scenario == "SIEMENS_GUARD_PERMIT"
        and test.output_tag == "State"
        and test.preconditions
        == {"DoorInterlock": True, "MotorReady": True, "Start": True}
        for test in result.fat_tests
    )
    assert any(
        test.scenario == "SIEMENS_GUARD_PATH_BLOCK"
        for test in result.fat_tests
    )
    assert any(
        check.id == "SIEMENS_V6_GUARD_TRACEABILITY"
        and check.status is StaticCheckStatus.PASS
        for check in result.static_checks
    )


def test_v6_explicit_requirement_maps_to_exact_transition_and_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "requirement.scl",
        _numeric_source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock AND MotorReady THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    requirements = _write(
        tmp_path / "requirements.txt",
        "REQ-START: State from 0 to 10 shall transition when Start = TRUE, DoorInterlock = TRUE, and MotorReady = TRUE.",
    )

    result = run_production_verification_v5(
        source,
        requirement_paths=(requirements,),
    )

    verification = next(
        item
        for item in result.requirement_verification
        if item.requirement_id == "REQ-START"
    )
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert "Exact Siemens V6 source transition proven" in verification.summary
    assert verification.linked_test_ids
    linked = {
        test.id: test
        for test in result.engineering.fat_tests
        if test.id in verification.linked_test_ids
    }
    assert linked
    assert any(
        test.scenario in {"SIEMENS_STATE_TRANSITION", "SIEMENS_GUARD_PERMIT"}
        for test in linked.values()
    )


def test_v6_requirement_conflicting_guard_is_not_false_verified(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "conflict.scl",
        _numeric_source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock AND MotorReady THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    requirements = _write(
        tmp_path / "requirements.txt",
        "REQ-CONFLICT: State from 0 to 10 shall transition when Start = TRUE, DoorInterlock = TRUE, and MotorReady = FALSE.",
    )

    result = run_production_verification_v5(
        source,
        requirement_paths=(requirements,),
    )
    verification = next(
        item
        for item in result.requirement_verification
        if item.requirement_id == "REQ-CONFLICT"
    )
    assert verification.status is RequirementStatus.CONFLICT


def test_v6_runtime_dependent_requirement_remains_traceable_not_proven(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "timer.scl",
        _numeric_source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        Delay(IN := TRUE, PT := T#1s);
        IF Delay.Q AND ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
""",
            declarations="    Delay : TON;",
        ),
    )
    requirements = _write(
        tmp_path / "requirements.txt",
        "REQ-TIMER: State from 10 to 0 shall transition when Delay.Q = TRUE and ResetCmd = TRUE.",
    )

    result = run_production_verification_v5(
        source,
        requirement_paths=(requirements,),
    )
    verification = next(
        item
        for item in result.requirement_verification
        if item.requirement_id == "REQ-TIMER"
    )

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "runtime dependency" in verification.summary


def test_v7_explicit_reset_transition_and_restart_fat_are_traced(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "recovery.scl",
        _numeric_source(
            """
CASE State OF
    0:
        IF Start AND DoorInterlock THEN
            State := 10;
        END_IF;
    10:
        IF ResetCmd THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v7(project)
    facts = project._siemens_v7_recovery_facts

    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["schema"] == "devagent-siemens-tia-capability-v7"
    assert profile["recovery_contract"] == "COMPLETE"
    assert profile["recovery_transitions"] == 1
    assert profile["fault_recovery_gaps"] == 0

    machine = facts.machines[0]
    assert len(machine.recovery_transitions) == 1
    recovery = machine.recovery_transitions[0]
    assert recovery.source_state == "10"
    assert recovery.target_state == "0"
    assert recovery.recovery_terms == ("ResetCmd",)

    assert any(
        test.scenario == "SIEMENS_FAULT_RECOVERY"
        and test.preconditions == {"ResetCmd": True}
        for test in result.engineering.fat_tests
    )
    assert any(
        test.scenario == "SIEMENS_RESTART_RETAINED_STATE"
        for test in result.engineering.fat_tests
    )
    assert any(
        check.id == "SIEMENS_V7_RESTART_RETENTION"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.engineering.static_checks
    )


def test_v7_named_fault_state_without_recovery_exit_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "fault_gap.scl",
        _named_source(
            """
CASE State OF
    IDLE:
        IF TripDetected THEN
            State := FAULT;
        END_IF;
    FAULT:
END_CASE;
"""
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v7(project)
    machine = project._siemens_v7_recovery_facts.machines[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["named_fault_states"] == 1
    assert profile["fault_recovery_gaps"] == 1
    assert profile["recovery_contract"] == "PARTIAL_FAIL_CLOSED"
    assert machine.fault_recovery_gaps == ("FAULT",)
    assert any(
        risk.category == "FAULT_RECOVERY"
        and "without an explicit recovery exit" in risk.title
        for risk in result.risks
    )
    assert any(
        test.scenario == "SIEMENS_FAULT_RECOVERY_GAP"
        for test in result.engineering.fat_tests
    )


def test_v7_named_fault_state_with_explicit_reset_exit_closes_topology_but_keeps_runtime_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "fault_recovery.scl",
        _named_source(
            """
CASE State OF
    IDLE:
        IF TripDetected THEN
            State := FAULT;
        END_IF;
    FAULT:
        IF ResetCmd THEN
            State := IDLE;
        END_IF;
END_CASE;
"""
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v7(project)

    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["named_fault_states"] == 1
    assert profile["recovery_transitions"] == 1
    assert profile["fault_recovery_gaps"] == 0
    assert profile["recovery_contract"] == "COMPLETE"
    assert profile["restart_retention_contract"] == "RUNTIME_REQUIRED"
    assert any(
        test.scenario == "SIEMENS_FAULT_RECOVERY"
        and test.execution_status == "NOT_RUN"
        for test in result.engineering.fat_tests
    )
