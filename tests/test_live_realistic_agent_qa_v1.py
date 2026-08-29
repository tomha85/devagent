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


async def _ask(scenario: str, *questions: str) -> tuple[str, ...]:
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
            replies = []
            for question in questions:
                reply = await assistant.answer(question)
                replies.append(reply.render_text())
            return tuple(replies)
        finally:
            await assistant.close()


def _answers(scenario: str, *questions: str) -> tuple[str, ...]:
    return asyncio.run(_ask(scenario, *questions))


def test_downstream_case_finds_exact_blocker_and_stops_at_unproven_input_cause() -> None:
    run, downstream = _answers(
        "downstream_blocker",
        "Why is RunCmd false?",
        "Why is DownstreamReady false?",
    )
    assert "Diagnosis: BLOCKER_IDENTIFIED" in run
    assert "Blocking condition(s): DownstreamReady" in run
    assert "RunCmd -> DownstreamReady" in run
    assert "Target: DownstreamReady" in downstream
    assert "Current value: False" in downstream
    assert "Deterministic writer: NOT FOUND" in downstream
    assert "cannot prove why this signal itself has its current value" in downstream


def test_drive_fault_case_finds_drive_fault_and_not_ready_conditions() -> None:
    (text,) = _answers("drive_fault", "Why is RunCmd false?")
    assert "Diagnosis: BLOCKER_IDENTIFIED" in text
    assert "DriveReady" in text
    assert "DriveFault" in text
    assert "DownstreamReady" not in text.split("Blocking condition(s):", 1)[1].split("\n", 1)[0]


def test_safety_trip_case_finds_both_safety_conditions() -> None:
    (text,) = _answers("safety_trip", "Why is RunCmd false?")
    assert "Diagnosis: BLOCKER_IDENTIFIED" in text
    blocker_line = text.split("Blocking condition(s):", 1)[1].split("\n", 1)[0]
    assert "SafetyOK" in blocker_line
    assert "SafetyTrip" in blocker_line


def test_multi_blocker_case_preserves_more_than_one_root_condition() -> None:
    (text,) = _answers("multi_blocker", "Which permissive is blocking RunCmd?")
    assert "Diagnosis: BLOCKER_IDENTIFIED" in text
    blocker_line = text.split("Blocking condition(s):", 1)[1].split("\n", 1)[0]
    assert "DriveReady" in blocker_line
    assert "DownstreamReady" in blocker_line


def test_logic_conflict_refuses_to_invent_a_blocker() -> None:
    (text,) = _answers("logic_conflict", "Why is RunCmd false?")
    assert "Diagnosis: LOGIC_CONFLICT" in text
    assert "All modeled conditions for RunCmd are currently satisfied" in text
    assert "Blocking condition(s):" not in text


def test_stuck_on_conflict_refuses_to_explain_true_output_with_false_permissive() -> None:
    (text,) = _answers("stuck_on_conflict", "Why is RunCmd true?")
    assert "Diagnosis: LOGIC_CONFLICT" in text
    assert "blocked, but the CURRENT output value is TRUE" in text


def test_idle_case_explains_no_start_request_without_calling_it_faulted() -> None:
    health, run = _answers(
        "idle",
        "Does the system have any faults?",
        "Why is RunCmd false?",
    )
    assert "Status: NO_CURRENT_PROVEN_FAULT" in health
    assert "Diagnosis: BLOCKER_IDENTIFIED" in run
    assert "Blocking condition(s): StartRequest" in run
    assert "fault" not in run.split("Blocking condition(s):", 1)[1].split("\n", 1)[0].casefold()
