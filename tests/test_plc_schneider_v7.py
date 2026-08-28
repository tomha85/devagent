from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState, StaticCheckStatus
from devagent.plc.production_report import render_production_report
from devagent.plc.schneider_fault_recovery_v7 import schneider_capability_profile_v7


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
  <contentHeader name="SchneiderV7" version="1.0" />
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
        ("StartCmd", "BOOL"),
        ("FaultDetected", "BOOL"),
        ("ResetCmd", "BOOL"),
        ("DoorInterlock", "BOOL"),
        ("AutoRestartEnable", "BOOL"),
        ("ManualRoute", "BOOL"),
        ("Done", "BOOL"),
    ]


def _safe_recovery_source() -> str:
    return _st(
        """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
ELSIF StartCmd THEN
State := 10;
END_IF
10:
IF Done THEN
State := 0;
ELSIF FaultDetected THEN
State := 900;
END_IF
900:
IF ResetCmd AND DoorInterlock THEN
State := 0;
END_IF
END_CASE
""",
        _vars(),
    )


def test_v7_proves_bounded_fault_latch_and_recovery_dominance(tmp_path: Path) -> None:
    source = _write(tmp_path / "SafeRecovery.xst", _safe_recovery_source())
    result = run_production_verification_v5(source)
    project = result.engineering.project
    profile = schneider_capability_profile_v7(project)
    facts = project._schneider_v7_recovery_facts

    assert project.metadata.schema_revision == "SCHNEIDER-CONTROL-EXPERT-EXPORT-V7"
    assert profile["schema"] == "devagent-schneider-control-expert-capability-v7"
    assert profile["recovery_contract"] == "COMPLETE"
    assert profile["fault_states"] == 1
    assert profile["fault_latched_states"] == 1
    assert profile["recovery_gaps"] == 0
    assert profile["recovery_bypass_exits"] == 0
    assert profile["stale_command_exit_hazards"] == 0
    assert facts.machines[0].fault_states == ("900",)
    assert facts.machines[0].fault_latched_states == ("900",)
    assert facts.machines[0].semantic_state is PLCSemanticState.FULL
    assert result.engineering.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert any(test.scenario == "SCHNEIDER_FAULT_ENTRY_V7" for test in result.engineering.fat_tests)
    assert any(test.scenario == "SCHNEIDER_FAULT_RECOVERY_V7" for test in result.engineering.fat_tests)
    assert any(test.scenario == "SCHNEIDER_RESTART_RETAINED_STATE_V7" for test in result.engineering.fat_tests)
    assert any(
        check.id == "SCHNEIDER_V7_FAULT_LATCH_DOMINANCE"
        and check.status is StaticCheckStatus.PASS
        for check in result.engineering.static_checks
    )


def test_v7_fault_state_without_recovery_dominated_exit_fails_closed(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "NoRecovery.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
900:
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)
    machine = result.engineering.project._schneider_v7_recovery_facts.machines[0]

    assert machine.fault_states == ("900",)
    assert machine.recovery_gaps == ("900",)
    assert profile["recovery_contract"] == "PARTIAL_FAIL_CLOSED"
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(test.scenario == "SCHNEIDER_FAULT_RECOVERY_GAP_V7" for test in result.engineering.fat_tests)
    assert any(risk.category == "FAULT_RECOVERY" and "lacks a recovery-dominated exit" in risk.title for risk in result.risks)


def test_v7_grouped_same_target_path_cannot_bypass_reset(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "RecoveryBypass.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
900:
IF ResetCmd THEN
State := 0;
END_IF
IF AutoRestartEnable THEN
State := 0;
END_IF
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)
    machine = result.engineering.project._schneider_v7_recovery_facts.machines[0]

    assert machine.fault_states == ("900",)
    assert profile["recovery_bypass_exits"] == 1
    assert profile["recovery_gaps"] == 1
    assert machine.fault_latched_states == ()
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert any(hazard.kind == "RECOVERY_BYPASS" for hazard in machine.exit_hazards)


def test_v7_detects_stale_command_exit_from_fault_state(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "StaleCommand.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
10:
IF FaultDetected THEN
State := 900;
END_IF
900:
IF ResetCmd THEN
State := 0;
END_IF
IF StartCmd THEN
State := 10;
END_IF
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)
    machine = result.engineering.project._schneider_v7_recovery_facts.machines[0]

    assert profile["stale_command_exit_hazards"] == 1
    stale = next(hazard for hazard in machine.exit_hazards if hazard.kind == "STALE_COMMAND_EXIT")
    assert "StartCmd" in stale.command_terms
    assert any(test.scenario == "SCHNEIDER_STALE_COMMAND_EXIT_V7" for test in result.engineering.fat_tests)
    assert any("Command-like path" in risk.title for risk in result.risks)


def test_v7_restart_name_alone_is_not_strong_recovery_authorization(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "RestartOnly.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
900:
IF AutoRestartEnable THEN
State := 0;
END_IF
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)
    machine = result.engineering.project._schneider_v7_recovery_facts.machines[0]

    assert profile["recovery_transitions"] == 0
    assert profile["stale_command_exit_hazards"] == 1
    assert profile["recovery_gaps"] == 1
    assert machine.fault_latched_states == ()


def test_v7_fault_label_must_dominate_every_incoming_path(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "FaultEntryBypass.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
END_IF
IF ManualRoute THEN
State := 900;
END_IF
900:
IF ResetCmd THEN
State := 0;
END_IF
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)

    assert profile["fault_entry_contracts"] == 0
    assert profile["fault_states"] == 0
    assert profile["unproven_recovery_sources"] == 1
    assert profile["recovery_contract"] == "PARTIAL_FAIL_CLOSED"


def test_v7_mixed_incoming_routes_make_fault_identity_ambiguous(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "AmbiguousFault.xst",
        _st(
            """
CASE State OF
0:
IF FaultDetected THEN
State := 900;
ELSIF StartCmd THEN
State := 10;
END_IF
10:
IF ManualRoute THEN
State := 900;
END_IF
900:
IF ResetCmd THEN
State := 0;
END_IF
END_CASE
""",
            _vars(),
        ),
    )
    result = run_production_verification_v5(source)
    profile = schneider_capability_profile_v7(result.engineering.project)
    machine = result.engineering.project._schneider_v7_recovery_facts.machines[0]

    assert profile["fault_state_candidates"] == 1
    assert profile["ambiguous_fault_states"] == 1
    assert profile["fault_states"] == 0
    assert machine.ambiguous_fault_states == ("900",)
    assert any(hazard.kind == "AMBIGUOUS_FAULT_STATE_ENTRY" for hazard in machine.exit_hazards)
    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED


def test_v7_report_and_evidence_expose_runtime_boundary(tmp_path: Path) -> None:
    source = _write(tmp_path / "Report.xst", _safe_recovery_source())
    result = run_production_verification_v5(source)
    report = render_production_report(result)

    assert "### Schneider V7 Fault / Reset / Recovery / Restart" in report
    assert "Restart` naming alone" in report
    assert "Control Expert Simulator" in report
    kinds = {item.kind for item in result.evidence}
    assert "SCHNEIDER_FAULT_RECOVERY_MACHINE_V7" in kinds
    assert "SCHNEIDER_FAULT_ENTRY_V7" in kinds
    assert "SCHNEIDER_FAULT_RECOVERY_V7" in kinds
    capability = next(item for item in result.evidence if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE")
    # The evidence item is the current-stack capability profile. V7-specific
    # evidence kinds above remain present, while the aggregate schema advances
    # through the merged V8/V9 production stack.
    assert capability.payload["schema"] == "devagent-schneider-control-expert-capability-v9"
    assert "Schneider V9" in capability.summary
