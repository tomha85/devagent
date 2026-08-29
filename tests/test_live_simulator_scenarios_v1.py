from __future__ import annotations

import pytest

from devagent.live.simulator_scenarios import (
    SIMULATOR_SCENARIO_NAMES,
    SIMULATOR_SCENARIOS,
    simulator_scenario,
)


def _modeled_run_expected(spec) -> bool:
    return bool(
        spec.auto_mode
        and spec.start_request
        and spec.safety_ok
        and not spec.safety_trip
        and spec.drive_ready
        and not spec.drive_fault
        and spec.downstream_ready
    )


def test_realistic_matrix_has_stable_named_cases() -> None:
    assert SIMULATOR_SCENARIO_NAMES == (
        "normal",
        "blocker",
        "healthy",
        "idle",
        "downstream_blocker",
        "drive_fault",
        "safety_trip",
        "multi_blocker",
        "logic_conflict",
        "stuck_on_conflict",
    )


def test_healthy_matches_modeled_logic_and_has_no_fault() -> None:
    spec = simulator_scenario("healthy")
    assert _modeled_run_expected(spec) is True
    assert spec.run_cmd is True
    assert spec.fault_code == 0
    assert spec.drive_fault is False
    assert spec.safety_trip is False
    assert spec.expected_system_health == "NO_CURRENT_PROVEN_FAULT"


def test_idle_is_intentional_inactivity_not_a_fault_case() -> None:
    spec = simulator_scenario("idle")
    assert spec.start_request is False
    assert _modeled_run_expected(spec) is False
    assert spec.run_cmd is False
    assert spec.fault_code == 0
    assert spec.machine_state == "IDLE"
    assert spec.expected_system_health == "NO_CURRENT_PROVEN_FAULT"


def test_downstream_blocker_is_consistent_blocked_logic() -> None:
    spec = simulator_scenario("downstream_blocker")
    assert spec.downstream_ready is False
    assert _modeled_run_expected(spec) is False
    assert spec.run_cmd is False
    assert spec.fault_code == 0
    assert spec.expected_system_health == "ATTENTION_REQUIRED"


def test_drive_fault_contains_explicit_fault_and_blocker_ground_truth() -> None:
    spec = simulator_scenario("drive_fault")
    assert spec.drive_fault is True
    assert spec.drive_ready is False
    assert spec.fault_code == 101
    assert _modeled_run_expected(spec) is False
    assert spec.run_cmd is False


def test_safety_trip_contains_explicit_safety_fault_ground_truth() -> None:
    spec = simulator_scenario("safety_trip")
    assert spec.safety_ok is False
    assert spec.safety_trip is True
    assert spec.fault_code == 201
    assert _modeled_run_expected(spec) is False
    assert spec.run_cmd is False


def test_multi_blocker_contains_two_independent_missing_permissives() -> None:
    spec = simulator_scenario("multi_blocker")
    assert spec.drive_ready is False
    assert spec.downstream_ready is False
    assert spec.drive_fault is False
    assert spec.fault_code == 0
    assert spec.run_cmd is False


def test_logic_conflict_intentionally_disagrees_with_modeled_output() -> None:
    spec = simulator_scenario("logic_conflict")
    assert _modeled_run_expected(spec) is True
    assert spec.run_cmd is False
    assert spec.fault_code == 0


def test_stuck_on_conflict_intentionally_disagrees_in_other_direction() -> None:
    spec = simulator_scenario("stuck_on_conflict")
    assert _modeled_run_expected(spec) is False
    assert spec.run_cmd is True
    assert spec.downstream_ready is False


def test_blocker_alias_remains_backward_compatible() -> None:
    blocker = simulator_scenario("blocker")
    explicit = simulator_scenario("downstream_blocker")
    assert blocker.downstream_ready == explicit.downstream_ready is False
    assert blocker.run_cmd == explicit.run_cmd is False


def test_unknown_scenario_fails_closed_with_choices() -> None:
    with pytest.raises(ValueError, match="unknown simulator scenario"):
        simulator_scenario("invented_case")


def test_every_fixed_case_declares_expected_health_and_reason() -> None:
    for name, spec in SIMULATOR_SCENARIOS.items():
        assert spec.description
        assert spec.expected_system_health
        assert spec.expected_primary_reason
        assert spec.ground_truth_lines()
        if name != "normal":
            assert spec.dynamic is False
