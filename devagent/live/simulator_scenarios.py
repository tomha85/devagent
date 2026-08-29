from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimulatorScenarioSpec:
    name: str
    description: str
    expected_system_health: str
    expected_primary_reason: str
    dynamic: bool = False
    auto_mode: bool = True
    start_request: bool = True
    safety_ok: bool = True
    safety_trip: bool = False
    drive_ready: bool = True
    drive_fault: bool = False
    downstream_ready: bool = True
    run_cmd: bool = True
    speed: float = 42.5
    fault_code: int = 0
    sorter_ready: bool = True
    home_sensor: bool = True
    machine_state: str = "RUNNING"

    def ground_truth_lines(self) -> tuple[str, ...]:
        return (
            f"AutoMode={self.auto_mode}",
            f"StartRequest={self.start_request}",
            f"SafetyOK={self.safety_ok}",
            f"SafetyTrip={self.safety_trip}",
            f"DriveReady={self.drive_ready}",
            f"DriveFault={self.drive_fault}",
            f"DownstreamReady={self.downstream_ready}",
            f"RunCmd={self.run_cmd}",
            f"FaultCode={self.fault_code}",
            f"MachineState={self.machine_state}",
        )


_SCENARIOS = (
    SimulatorScenarioSpec(
        name="normal",
        description=(
            "Backward-compatible dynamic qualification mode. DownstreamReady and RunCmd "
            "periodically transition so browse/read/watch/history paths see real changes."
        ),
        expected_system_health="TRANSITIONING",
        expected_primary_reason="Dynamic qualification scenario; evaluate the current sample, not a fixed health result.",
        dynamic=True,
    ),
    SimulatorScenarioSpec(
        name="blocker",
        description="Backward-compatible fixed downstream-not-ready blocker.",
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="DownstreamReady is FALSE and blocks RunCmd.",
        downstream_ready=False,
        run_cmd=False,
        sorter_ready=False,
        home_sensor=False,
        machine_state="BLOCKED",
    ),
    SimulatorScenarioSpec(
        name="healthy",
        description="Steady-state healthy automatic operation with all modeled permissives satisfied.",
        expected_system_health="NO_CURRENT_PROVEN_FAULT",
        expected_primary_reason="No active fault, operational blocker, or deterministic logic conflict.",
    ),
    SimulatorScenarioSpec(
        name="idle",
        description=(
            "Operator has not requested a run. RunCmd is correctly FALSE, but this is normal "
            "command-driven inactivity and must not be called a machine fault."
        ),
        expected_system_health="NO_CURRENT_PROVEN_FAULT",
        expected_primary_reason="StartRequest is FALSE; inactivity is intentional, not a proven fault.",
        start_request=False,
        run_cmd=False,
        speed=0.0,
        machine_state="IDLE",
    ),
    SimulatorScenarioSpec(
        name="downstream_blocker",
        description="Conveyor is requested to run but downstream equipment is not ready.",
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="DownstreamReady is FALSE and blocks RunCmd.",
        downstream_ready=False,
        run_cmd=False,
        sorter_ready=False,
        home_sensor=False,
        speed=0.0,
        machine_state="BLOCKED",
    ),
    SimulatorScenarioSpec(
        name="drive_fault",
        description="Drive reports an active fault and is not ready; the conveyor cannot run.",
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="DriveFault is TRUE, DriveReady is FALSE, and FaultCode is non-zero.",
        drive_ready=False,
        drive_fault=True,
        run_cmd=False,
        speed=0.0,
        fault_code=101,
        machine_state="FAULTED",
    ),
    SimulatorScenarioSpec(
        name="safety_trip",
        description="Safety chain is not OK and a safety trip is active.",
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="SafetyOK is FALSE and SafetyTrip is TRUE.",
        safety_ok=False,
        safety_trip=True,
        run_cmd=False,
        speed=0.0,
        fault_code=201,
        machine_state="FAULTED",
    ),
    SimulatorScenarioSpec(
        name="multi_blocker",
        description="More than one operational permissive is missing at the same time.",
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="DriveReady and DownstreamReady are both FALSE.",
        drive_ready=False,
        downstream_ready=False,
        run_cmd=False,
        sorter_ready=False,
        home_sensor=False,
        speed=0.0,
        machine_state="BLOCKED",
    ),
    SimulatorScenarioSpec(
        name="logic_conflict",
        description=(
            "All modeled RunCmd conditions are satisfied but the observed RunCmd is FALSE. "
            "The agent must report a deterministic logic/runtime conflict, not invent a blocker."
        ),
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="Modeled logic expects RunCmd TRUE while trusted CURRENT RunCmd is FALSE.",
        run_cmd=False,
        speed=0.0,
        machine_state="RUNNING",
    ),
    SimulatorScenarioSpec(
        name="stuck_on_conflict",
        description=(
            "DownstreamReady is FALSE but observed RunCmd remains TRUE. The agent must report "
            "a logic/runtime conflict instead of claiming normal operation."
        ),
        expected_system_health="ATTENTION_REQUIRED",
        expected_primary_reason="Modeled logic expects RunCmd FALSE while trusted CURRENT RunCmd is TRUE.",
        downstream_ready=False,
        run_cmd=True,
        sorter_ready=False,
        home_sensor=False,
        speed=42.5,
        machine_state="RUNNING",
    ),
)


SIMULATOR_SCENARIOS = {item.name: item for item in _SCENARIOS}
SIMULATOR_SCENARIO_NAMES = tuple(item.name for item in _SCENARIOS)


def simulator_scenario(name: str) -> SimulatorScenarioSpec:
    key = str(name or "").strip()
    try:
        return SIMULATOR_SCENARIOS[key]
    except KeyError as exc:
        choices = ", ".join(SIMULATOR_SCENARIO_NAMES)
        raise ValueError(f"unknown simulator scenario {key!r}; choose one of: {choices}") from exc


__all__ = [
    "SIMULATOR_SCENARIOS",
    "SIMULATOR_SCENARIO_NAMES",
    "SimulatorScenarioSpec",
    "simulator_scenario",
]
