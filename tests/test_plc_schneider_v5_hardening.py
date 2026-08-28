from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.schneider_state_machine_v5 import schneider_capability_profile_v5


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _st(body: str, variables: list[tuple[str, str]], *, name: str = "Hardening") -> str:
    tags = "\n".join(
        f'    <variables name="{tag}" typeName="{dtype}" />'
        for tag, dtype in variables
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV5Hardening" version="1.0" />
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


def test_v5_named_state_constant_is_fail_closed_until_v8_identity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Named.xst",
        _st(
            """
CASE State OF
IDLE:
IF Start THEN
State := RUNNING;
END_IF
RUNNING:
END_CASE
""",
            [("State", "INT"), ("Start", "BOOL")],
            name="NamedStates",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert machine.transitions == ()
    assert machine.reason in {"invalid_state_label", "state_target_must_be_simple_literal"}


def test_v5_numeric_base_literals_are_canonicalized_to_same_state(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Base.xst",
        _st(
            """
CASE State OF
16#0A:
IF Start THEN
State := 10;
END_IF
10:
END_CASE
""",
            [("State", "INT"), ("Start", "BOOL")],
            name="BaseStates",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "duplicate_state_label:10" in machine.reason


def test_v5_out_of_range_state_value_is_partial(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Range.xst",
        _st(
            """
CASE State OF
0:
IF Start THEN
State := 40000;
END_IF
40000:
END_CASE
""",
            [("State", "INT"), ("Start", "BOOL")],
            name="RangeCheck",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "state_value_out_of_range" in machine.reason
    assert all(
        item.semantic_state is PLCSemanticState.PARTIAL
        for item in machine.transitions
    )


def test_v5_runtime_output_binding_to_state_is_fail_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "TimerOutput.xst",
        _st(
            """
CASE State OF
0:
Timer1(IN := Start, PT := T#1s, Q => State);
IF Timer1.Q THEN
State := 1;
END_IF
1:
END_CASE
""",
            [("State", "INT"), ("Start", "BOOL"), ("Timer1", "TON")],
            name="TimerOutput",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert "runtime_call_output_binding_unsupported" in machine.reason


def test_v5_impossible_transition_guard_is_not_promoted(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "FalseGuard.xst",
        _st(
            """
CASE State OF
0:
IF A AND NOT A THEN
State := 1;
END_IF
1:
END_CASE
""",
            [("State", "INT"), ("A", "BOOL")],
            name="FalseGuard",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert machine.transitions == ()
    assert "transition_guard_never_true" in machine.reason


def test_v5_own_unmodeled_case_write_is_not_misclassified_as_competing_writer(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "OwnUnsupported.xst",
        _st(
            """
CASE State OF
0:
State := State + 1;
1:
END_CASE
""",
            [("State", "INT")],
            name="OwnUnsupported",
        ),
    )
    result = run_production_verification_v5(source)
    machine = getattr(result.engineering.project, "_schneider_v5_facts").machines[0]

    assert machine.semantic_state is PLCSemanticState.PARTIAL
    assert machine.writer_conflicts == ()
    assert "competing_state_writer" not in machine.reason


def test_v5_internal_transition_writers_do_not_emit_generic_multi_writer_risk(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "Sequence.xst",
        _st(
            """
CASE State OF
0:
IF A THEN
State := 1;
END_IF
1:
IF B THEN
State := 2;
END_IF
2:
END_CASE
""",
            [("State", "INT"), ("A", "BOOL"), ("B", "BOOL")],
            name="InternalWriters",
        ),
    )
    result = run_production_verification_v5(source)

    assert not any(
        risk.category == "MULTIPLE_WRITERS"
        and risk.title == "Multiple Schneider source writers for State"
        for risk in result.risks
    )


def test_v5_capability_evidence_and_findings_are_version_correct(tmp_path: Path) -> None:
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
            [("State", "INT"), ("Start", "BOOL")],
            name="VersionText",
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v5(result.engineering.project)
    capability = next(
        item for item in result.evidence
        if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
    )

    assert profile["schema"] == "devagent-schneider-control-expert-capability-v5"
    assert "V5" in capability.summary
    assert all(
        "Schneider Control Expert V1" not in finding.title
        and "Schneider Control Expert V1" not in finding.summary
        and "Schneider V1" not in finding.title
        and "Schneider V1" not in finding.summary
        for finding in result.engineering_findings
    )
