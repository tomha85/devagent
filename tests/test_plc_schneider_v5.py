from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_state_machine_v5 import schneider_capability_profile_v5


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _st(body: str, variables: list[tuple[str, str]], *, name: str = "Sequence") -> str:
    tags = "\n".join(
        f'    <variables name="{tag}" typeName="{dtype}" />'
        for tag, dtype in variables
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV5" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <STSource>
{body.strip()}
    </STSource>
  </program>
  <dataBlock>
{tags}
  </dataBlock>
</STExchangeFile>
"""


def _base_vars():
    return [
        ("State", "INT"),
        ("Start", "BOOL"),
        ("Done", "BOOL"),
        ("Fault", "BOOL"),
        ("A", "BOOL"),
        ("B", "BOOL"),
    ]


def test_schneider_v5_models_bounded_case_transition_graph(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 10;
END_IF
10:
IF Done THEN
State := 20;
END_IF
20:
END_CASE
""",
            _base_vars(),
        ),
    )
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile_v5(project)
    facts = getattr(project, "_schneider_v5_facts")

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v5"
    assert profile["state_machines"] == 1
    assert profile["state_machine_full"] == 1
    assert profile["state_machine_states"] == 3
    assert profile["state_machine_transitions"] == 2
    assert profile["state_machine_dangling_targets"] == 0
    assert profile["state_machine_overlap_conflicts"] == 0
    assert profile["state_machine_terminal_states"] == 1
    assert facts.machines[0].semantic_state is PLCSemanticState.FULL
    assert [(item.source_state, item.target_state) for item in facts.machines[0].transitions] == [
        ("0", "10"),
        ("10", "20"),
    ]
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert any(item.scenario == "SCHNEIDER_STATE_STARTUP" for item in result.engineering.fat_tests)
    assert len([item for item in result.engineering.fat_tests if item.scenario == "SCHNEIDER_STATE_TRANSITION"]) == 2
    assert all(item.execution_status == "NOT_RUN" for item in result.engineering.fat_tests)


def test_schneider_v5_elsif_priority_is_mutually_exclusive(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Priority.xst",
        _st(
            """
CASE State OF
0:
IF A THEN
State := 1;
ELSIF B THEN
State := 2;
END_IF
1:
2:
END_CASE
""",
            _base_vars(),
            name="Priority",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v5(result.engineering.project)
    facts = getattr(result.engineering.project, "_schneider_v5_facts")
    machine = facts.machines[0]

    assert profile["state_machine_overlap_conflicts"] == 0
    assert machine.semantic_state is PLCSemanticState.FULL
    assert len(machine.transitions) == 2
    second = machine.transitions[1]
    assert {name: value for name, value in second.guard_paths[0]} == {"A": False, "B": True}


def test_schneider_v5_overlapping_independent_transitions_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Overlap.xst",
        _st(
            """
CASE State OF
0:
IF A THEN
State := 1;
END_IF
IF B THEN
State := 2;
END_IF
1:
2:
END_CASE
""",
            _base_vars(),
            name="Overlap",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v5(result.engineering.project)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert profile["state_machine_overlap_conflicts"] == 1
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(item.category == "SEQUENCE_AMBIGUITY" for item in result.risks)
    assert any(item.scenario == "SCHNEIDER_STATE_GAP" for item in result.engineering.fat_tests)


def test_schneider_v5_dangling_target_is_not_proven(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Dangling.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 99;
END_IF
1:
END_CASE
""",
            _base_vars(),
            name="Dangling",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v5(result.engineering.project)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert profile["state_machine_dangling_targets"] == 1
    assert machine.dangling_targets == ("99",)
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert any(item.category == "SEQUENCE_GAP" for item in result.risks)


def test_schneider_v5_competing_state_writer_blocks_machine(tmp_path: Path) -> None:
    root = tmp_path / "export"
    root.mkdir()
    _write(
        root / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 1;
END_IF
1:
END_CASE
""",
            _base_vars(),
            name="Sequence",
        ),
    )
    _write(
        root / "Override.xst",
        _st(
            """
State := 7;
""",
            [("State", "INT")],
            name="Override",
        ),
    )

    result = run_production_verification_v5(root)
    profile = schneider_capability_profile_v5(result.engineering.project)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert profile["state_machine_writer_conflicts"] >= 1
    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "competing_state_writer" in machine.reason
    assert any(item.category == "MULTIPLE_WRITERS" for item in result.risks)


def test_schneider_v5_nested_if_does_not_leak_inner_transition_proof(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Nested.xst",
        _st(
            """
CASE State OF
0:
IF A THEN
IF B THEN
State := 1;
END_IF
END_IF
1:
END_CASE
""",
            _base_vars(),
            name="Nested",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "nested_if_in_transition" in machine.reason
    assert machine.transitions == ()
    assert any(item.scenario == "SCHNEIDER_STATE_GAP" for item in result.engineering.fat_tests)


def test_schneider_v5_timer_guard_is_runtime_dependency_not_runtime_pass(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "TimerSequence.xst",
        _st(
            """
CASE State OF
0:
Timer1(IN := Start, PT := T#1s);
IF Timer1.Q THEN
State := 1;
END_IF
1:
END_CASE
""",
            [
                ("State", "INT"),
                ("Start", "BOOL"),
                ("Timer1", "TON"),
            ],
            name="TimerSequence",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v5(result.engineering.project)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]
    runtime_check = next(
        item for item in result.engineering.static_checks
        if item.id == "SCHNEIDER_V5_SEQUENCE_RUNTIME"
    )

    assert machine.semantic_state is PLCSemanticState.FULL
    assert machine.runtime_dependencies == ("Timer1:TON",)
    assert profile["state_machine_runtime_dependencies"] == 1
    assert runtime_check.status.value == "NOT_PROVEN"
    assert any(item.category == "STATEFUL_LOGIC" for item in result.risks)
    assert any(
        "Timer1:TON" in item.expected
        for item in result.engineering.fat_tests
        if item.scenario == "SCHNEIDER_STATE_TRANSITION"
    )


def test_schneider_v5_case_else_remains_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Else.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 1;
END_IF
1:
ELSE
State := 0;
END_CASE
""",
            _base_vars(),
            name="ElseCase",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert machine.has_default_branch is True
    assert "case_else_behavior_not_modeled" in machine.reason


def test_schneider_v5_sequence_requirement_stays_traceable_until_runtime_evidence(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 1;
END_IF
1:
END_CASE
""",
            _base_vars(),
            name="RequirementSequence",
        ),
    )
    req = _write(
        tmp_path / "requirements.md",
        "REQ-SCH-V5-001: When Start=TRUE, State shall transition from 0 to 1.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "Schneider V5" in verification.summary
    assert any(item.startswith("SCHNEIDER-SM5-") for item in verification.evidence_ids)


def test_schneider_v5_report_exposes_sequence_and_runtime_boundary(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 1;
END_IF
1:
END_CASE
""",
            _base_vars(),
            name="ReportSequence",
        ),
    )
    result = run_production_verification_v5(source)
    report = render_production_report(result)

    assert "### Schneider V5 Sequencing / State Machines" in report
    assert "bounded source transition relation" in report
    assert "Control Expert Simulator" in report
    assert "real Modicon" in report
