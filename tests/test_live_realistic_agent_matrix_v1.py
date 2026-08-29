from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

pytest.importorskip("asyncua")

from devagent.live.manager import PlcConnectionSpec
from devagent.live.recursive_assistant import create_recursive_live_commissioning_assistant
from devagent.live.security import LiveSecurityConfig
from devagent.live.simulator import OpcUaSimulator


PROJECT = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "live"
    / "warehouse_commissioning_demo.L5X"
)


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/simulator/"


async def _ask_health(scenario: str) -> str:
    endpoint = _free_endpoint()
    async with OpcUaSimulator(endpoint, scenario=scenario, update_interval_seconds=0.05):
        assistant = create_recursive_live_commissioning_assistant(
            PROJECT,
            PlcConnectionSpec(
                plc_id="sim1",
                plc_name="Warehouse Commissioning Simulator",
                endpoint=endpoint,
                security=LiveSecurityConfig(),
            ),
            browse_max_depth=5,
            browse_max_nodes=500,
            history_seconds=0,
        )
        try:
            reply = await assistant.answer("Does the system have any faults?")
            return reply.render_text()
        finally:
            await assistant.close()


def _health(scenario: str) -> str:
    return asyncio.run(_ask_health(scenario))


def test_healthy_reports_no_current_proven_fault() -> None:
    text = _health("healthy")
    assert "DEVAGENT LIVE SYSTEM HEALTH" in text
    assert "Status: NO_CURRENT_PROVEN_FAULT" in text
    assert "Current proven/observed issues: NONE" in text
    assert "FaultCode = 0" in text


def test_idle_does_not_turn_no_start_request_into_machine_fault() -> None:
    text = _health("idle")
    assert "Status: NO_CURRENT_PROVEN_FAULT" in text
    assert "RunCmd: blocked by StartRequest" in text
    assert "not classified as a system fault" in text
    assert "[FAULT]" not in text


def test_downstream_blocker_identifies_operational_permissive() -> None:
    text = _health("downstream_blocker")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[BLOCKER] RunCmd" in text
    assert "DownstreamReady" in text
    assert "FaultCode = 0" in text


def test_drive_fault_reports_explicit_fault_and_logic_blocker() -> None:
    text = _health("drive_fault")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[FAULT] DriveFault" in text
    assert "[FAULT] FaultCode" in text
    assert "101" in text
    assert "DriveReady" in text


def test_safety_trip_reports_explicit_safety_fault() -> None:
    text = _health("safety_trip")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[FAULT] SafetyTrip" in text
    assert "[FAULT] FaultCode" in text
    assert "201" in text
    assert "SafetyOK" in text


def test_multi_blocker_keeps_multiple_missing_permissives() -> None:
    text = _health("multi_blocker")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[BLOCKER] RunCmd" in text
    assert "DriveReady" in text
    assert "DownstreamReady" in text


def test_logic_conflict_is_not_rewritten_as_fake_blocker() -> None:
    text = _health("logic_conflict")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[CONFLICT] RunCmd" in text
    assert "conditions for RunCmd are currently satisfied" in text
    assert "[BLOCKER] RunCmd" not in text


def test_stuck_on_conflict_is_not_called_healthy() -> None:
    text = _health("stuck_on_conflict")
    assert "Status: ATTENTION_REQUIRED" in text
    assert "[CONFLICT] RunCmd" in text
    assert "blocked, but the CURRENT output value is TRUE" in text
    assert "Status: NO_CURRENT_PROVEN_FAULT" not in text
