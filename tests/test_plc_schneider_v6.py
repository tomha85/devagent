from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus
from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_interlock_permissive_v6 import schneider_capability_profile_v6


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _st(body: str, variables: list[tuple[str, str]], *, name: str = "V6") -> str:
    tags = "\n".join(
        f'    <variables name="{tag}" typeName="{dtype}" />'
        for tag, dtype in variables
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV6" version="1.0" />
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


def _vars():
    return [
        ("State", "INT"),
        ("Start", "BOOL"),
        ("Auto", "BOOL"),
        ("DoorInterlock", "BOOL"),
        ("MotorReady", "BOOL"),
        ("Bypass", "BOOL"),
        ("AlreadyDone", "BOOL"),
        ("FaultInterlock", "BOOL"),
        ("MotorRun", "BOOL"),
    ]


def test_v6_output_guard_dominance_classification_and_fat(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutputGuard.xst",
        _st(
            "MotorRun := Start AND DoorInterlock AND MotorReady;",
            _vars(),
            name="OutputGuard",
        ),
    )
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile_v6(project)
    facts = project._schneider_v6_guard_facts

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v6"
    assert profile["output_guard_contracts"] == 1
    assert profile["transition_guard_contracts"] == 0
    assert profile["guard_contract"] == "COMPLETE"
    assert profile["classified_interlock_terms"] == 1
    assert profile["classified_permissive_terms"] == 1

    contract = facts.output_contracts[0]
    assert contract.semantic_state is PLCSemanticState.FULL
    assert dict(contract.all_path_terms) == {
        "DoorInterlock": True,
        "MotorReady": True,
        "Start": True,
    }
    roles = {term.tag: term.role for term in contract.terms}
    assert roles["DoorInterlock"] == "INTERLOCK"
    assert roles["MotorReady"] == "PERMISSIVE"
    assert roles["Start"] == "GUARD"
    assert any(
        test.scenario == "SCHNEIDER_OUTPUT_GUARD_ALL_PATH_BLOCK"
        and test.output_tag == "MotorRun"
        and test.preconditions == {"DoorInterlock": False}
        for test in result.engineering.fat_tests
    )
    assert any(
        check.id == "SCHNEIDER_V6_ALL_PATH_GUARD_DOMINANCE"
        and check.status is StaticCheckStatus.PASS
        for check in result.engineering.static_checks
    )


def test_v6_token_classifier_does_not_match_substrings(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Token.xst",
        _st(
            "MotorRun := AlreadyDone;",
            _vars(),
            name="Token",
        ),
    )
    result = run_production_verification_v5(source)
    contract = result.engineering.project._schneider_v6_guard_facts.output_contracts[0]
    roles = {term.tag: term.role for term in contract.terms}

    assert roles["AlreadyDone"] == "GUARD"


def test_v6_restrictive_output_requirement_proves_every_path(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutputReq.xst",
        _st(
            "MotorRun := Start AND DoorInterlock AND MotorReady;",
            _vars(),
            name="OutputReq",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-OUT-V6: MotorRun shall only be TRUE when DoorInterlock = TRUE and MotorReady = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    verification = result.requirement_verification[0]

    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert "every-path output guard theorem" in verification.summary
    assert any(
        test_id.startswith("FAT-SCHNEIDER-GUARD6-")
        for test_id in verification.linked_test_ids
    )


def test_v6_output_bypass_path_conflicts_with_mandatory_guard_requirement(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OutputBypass.xst",
        _st(
            "MotorRun := (Start AND DoorInterlock) OR Bypass;",
            _vars(),
            name="OutputBypass",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-BYPASS-V6: MotorRun shall only be TRUE when DoorInterlock = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    verification = result.requirement_verification[0]
    contract = result.engineering.project._schneider_v6_guard_facts.output_contracts[0]

    assert verification.status is RequirementStatus.CONFLICT
    assert "DoorInterlock" not in dict(contract.all_path_terms)
    assert any(risk.category == "INTERLOCK_COVERAGE" for risk in result.risks)


def test_v6_guard_absent_from_output_theorem_is_conflict_not_false_pass(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "MissingGuard.xst",
        _st(
            "MotorRun := Start;",
            _vars(),
            name="MissingGuard",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-MISSING-V6: MotorRun shall only be TRUE when DoorInterlock = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])

    assert result.requirement_verification[0].status is RequirementStatus.CONFLICT


def test_v6_groups_separate_same_target_transitions_before_every_path_proof(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "TransitionGroup.xst",
        _st(
            """
CASE State OF
0:
IF Start AND DoorInterlock THEN
State := 10;
END_IF
IF Auto AND DoorInterlock THEN
State := 10;
END_IF
10:
END_CASE
""",
            _vars(),
            name="TransitionGroup",
        ),
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._schneider_v6_guard_facts
    contracts = [
        contract
        for contract in facts.transition_contracts
        if contract.source_state == "0" and contract.target_state == "10"
    ]

    assert len(contracts) == 1
    contract = contracts[0]
    assert len(contract.transition_ids) == 2
    assert len(contract.guard_paths) == 2
    assert dict(contract.all_path_terms) == {"DoorInterlock": True}
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED


def test_v6_restrictive_transition_requirement_proves_all_source_paths(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "TransitionReq.xst",
        _st(
            """
CASE State OF
0:
IF Start AND DoorInterlock THEN
State := 10;
END_IF
IF Auto AND DoorInterlock THEN
State := 10;
END_IF
10:
END_CASE
""",
            _vars(),
            name="TransitionReq",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-TRANS-V6: State from 0 to 10 shall only transition when DoorInterlock = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    verification = result.requirement_verification[0]

    assert verification.status is RequirementStatus.STATICALLY_VERIFIED
    assert "every-path transition guard theorem" in verification.summary
    assert any(
        result.engineering.fat_tests_by_id[test_id].scenario == "SCHNEIDER_GUARD_ALL_PATH_BLOCK"
        if hasattr(result.engineering, "fat_tests_by_id")
        else test_id.startswith("FAT-SCHNEIDER-GUARD6-")
        for test_id in verification.linked_test_ids
    )


def test_v6_transition_bypass_path_conflicts_with_mandatory_interlock(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "TransitionBypass.xst",
        _st(
            """
CASE State OF
0:
IF Start AND DoorInterlock THEN
State := 10;
END_IF
IF Bypass THEN
State := 10;
END_IF
10:
END_CASE
""",
            _vars(),
            name="TransitionBypass",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-TRANS-BYPASS: State from 0 to 10 shall only transition when DoorInterlock = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    contract = next(
        item
        for item in result.engineering.project._schneider_v6_guard_facts.transition_contracts
        if item.source_state == "0" and item.target_state == "10"
    )

    assert result.requirement_verification[0].status is RequirementStatus.CONFLICT
    assert "DoorInterlock" not in dict(contract.all_path_terms)
    assert any(risk.category == "INTERLOCK_COVERAGE" for risk in result.risks)


def test_v6_source_polarity_is_used_without_guessing_safe_polarity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Polarity.xst",
        _st(
            """
CASE State OF
0:
IF Start AND NOT FaultInterlock THEN
State := 10;
END_IF
10:
END_CASE
""",
            _vars(),
            name="Polarity",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-POLARITY: State from 0 to 10 shall only transition when FaultInterlock = FALSE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    contract = result.engineering.project._schneider_v6_guard_facts.transition_contracts[0]
    role = next(term.role for term in contract.terms if term.tag == "FaultInterlock")

    assert role == "INTERLOCK"
    assert dict(contract.all_path_terms)["FaultInterlock"] is False
    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED


def test_v6_timer_dependent_transition_remains_runtime_not_proven(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Timer.xst",
        _st(
            """
CASE State OF
0:
Timer1(IN := Start, PT := T#1s);
IF Timer1.Q AND DoorInterlock THEN
State := 10;
END_IF
10:
END_CASE
""",
            [*_vars(), ("Timer1", "TON")],
            name="Timer",
        ),
    )
    req = _write(
        tmp_path / "requirements.txt",
        "REQ-TIMER-V6: State from 0 to 10 shall only transition when Timer1.Q = TRUE and DoorInterlock = TRUE.",
    )
    result = run_production_verification_v5(source, requirement_paths=[req])
    contract = result.engineering.project._schneider_v6_guard_facts.transition_contracts[0]

    assert contract.runtime_dependencies == ("Timer1:TON",)
    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "runtime dependency" in result.requirement_verification[0].summary


def test_v6_report_exposes_every_path_boundary(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Report.xst",
        _st(
            "MotorRun := Start AND DoorInterlock AND MotorReady;",
            _vars(),
            name="Report",
        ),
    )
    result = run_production_verification_v5(source)
    report = render_production_report(result)

    assert "Schneider V6 Interlocks / Permissives / Every-Path Guard Proof" in report
    assert "All-path guard terms" in report
    assert "SIL/PL" in report
    assert "Control Expert Simulator" in report
