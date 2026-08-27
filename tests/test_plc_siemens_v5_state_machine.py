from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus
from devagent.plc.siemens_state_machine_v5 import siemens_capability_profile_v5


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _source(body: str, declarations: str = "") -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : Int;
    Start : Bool;
    Done : Bool;
    Reset : Bool;
{declarations}
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def test_v5_bounded_case_state_machine_models_exclusive_boolean_transitions(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "main.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        IF Done THEN
            State := 20;
        ELSIF Reset THEN
            State := 0;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )

    result = analyze_plc_project(source)
    project = result.project
    profile = siemens_capability_profile_v5(project)
    facts = project._siemens_v5_state_machine_facts

    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert profile["state_machines"] == 1
    assert profile["state_machine_contract"] == "COMPLETE"
    assert profile["state_machine_states"] == 3
    assert profile["state_machine_transitions"] == 4
    assert profile["state_machine_dangling_targets"] == 0
    assert profile["state_machine_overlap_conflicts"] == 0

    machine = facts.machines[0]
    assert machine.state_tag == "State"
    assert machine.state_type == "INT"
    assert machine.states == ("0", "10", "20")
    assert machine.semantic_state is PLCSemanticState.FULL

    transitions = {
        (item.source_state, item.target_state): item
        for item in machine.transitions
    }
    assert set(transitions) == {
        ("0", "10"),
        ("10", "20"),
        ("10", "0"),
        ("20", "0"),
    }
    assert transitions[("0", "10")].guard_paths == ((("Start", True),),)
    assert transitions[("10", "20")].guard_paths == ((("Done", True),),)
    assert transitions[("10", "0")].guard_paths == (
        (("Done", False), ("Reset", True)),
    )

    assert project.st_statement_total == project.st_statement_semantic_count
    assert all(
        statement.semantic_state is PLCSemanticState.FULL
        for statement in project.logic_statements
        if statement.language == "SCL"
    )

    assert any(
        edge.kind == "DEPENDS_ON"
        and edge.source.casefold() == "state"
        and edge.target.casefold() == "start"
        for edge in result.graph.edges
    )
    assert any(
        check.id == "SIEMENS_V5_TRANSITION_DETERMINISM"
        and check.status is StaticCheckStatus.PASS
        for check in result.static_checks
    )
    assert any(
        test.scenario == "SIEMENS_STATE_TRANSITION"
        and "0 -> 10" in test.title
        for test in result.fat_tests
    )
    assert any(
        test.scenario == "SIEMENS_STATE_STARTUP"
        for test in result.fat_tests
    )


def test_v5_dangling_transition_target_fails_closed_and_creates_sequence_risk(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "dangling.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
END_CASE;
"""
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v5(project)
    machine = project._siemens_v5_state_machine_facts.machines[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["state_machine_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["state_machine_dangling_targets"] == 1
    assert machine.dangling_targets == ("10",)
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert any(
        risk.category == "SEQUENCE_GAP"
        and "undefined CASE states" in risk.title
        for risk in result.risks
    )
    assert any(
        test.scenario == "SIEMENS_STATE_GAP"
        for test in result.engineering.fat_tests
    )


def test_v5_overlapping_different_target_transitions_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "overlap.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
        IF Start THEN
            State := 20;
        END_IF;
    10:
        IF Reset THEN
            State := 0;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v5(project)
    machine = project._siemens_v5_state_machine_facts.machines[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["state_machine_overlap_conflicts"] == 1
    assert machine.overlap_conflicts
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert any(
        risk.category == "SEQUENCE_AMBIGUITY"
        for risk in result.risks
    )
    assert any(
        check.id == "SIEMENS_V5_TRANSITION_DETERMINISM"
        and check.status is StaticCheckStatus.NOT_PROVEN
        for check in result.engineering.static_checks
    )


def test_v5_timer_dependent_transition_is_traced_but_runtime_fat_remains_required(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "timer.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        Delay(IN := TRUE, PT := T#1s);
        IF Delay.Q THEN
            State := 20;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
""",
            declarations="    Delay : TON;",
        ),
    )

    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = siemens_capability_profile_v5(project)
    machine = project._siemens_v5_state_machine_facts.machines[0]

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert profile["state_machines"] == 1
    assert profile["state_machine_runtime_dependencies"] == 1
    assert machine.runtime_dependencies == ("Delay:TON",)

    timed = next(
        transition
        for transition in machine.transitions
        if transition.source_state == "10"
    )
    assert timed.target_state == "20"
    assert timed.runtime_dependencies == ("Delay:TON",)
    assert timed.guard_paths == ((("Delay.Q", True),),)
    assert any(
        statement.calls
        and statement.semantic_state is not PLCSemanticState.FULL
        for statement in project.logic_statements
    )
    assert any(
        test.scenario == "SIEMENS_STATE_TRANSITION"
        and "Runtime dependency: Delay:TON" in test.expected
        for test in result.engineering.fat_tests
    )
    assert any(
        risk.category == "STATEFUL_LOGIC"
        and "runtime timer/counter" in risk.title
        for risk in result.risks
    )


def test_v5_case_else_behavior_is_not_silently_promoted(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "case_else.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        IF Reset THEN
            State := 0;
        END_IF;
    ELSE
        State := 0;
END_CASE;
"""
        ),
    )

    result = analyze_plc_project(source)
    machine = result.project._siemens_v5_state_machine_facts.machines[0]

    assert result.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert machine.has_default_branch is True
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "case_else_behavior_not_modeled" in machine.reason
    assert any(
        test.scenario == "SIEMENS_STATE_GAP"
        for test in result.fat_tests
    )
