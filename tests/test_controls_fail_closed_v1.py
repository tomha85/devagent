from __future__ import annotations

import copy

import pytest

from devagent.controls.parser import load_control_system_spec
from devagent.controls.schema import ControlSpecError, parse_control_system_payload


def _payload():
    return {
        "schema": "devagent-controls-spec-v2",
        "project_id": "PACK01",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
        ],
        "equipment": [
            {
                "id": "CONV_101",
                "type": "CONVEYOR",
                "standard": "conveyor-v1",
                "controller": "PLC1",
                "signals": ["SAFE", "GUARD_OPEN", "DRIVE_FAULT"],
                "commands": {"START": True},
                "status": ["RUNNING"],
                "permissives": ["SAFE"],
                "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                "faults": ["DRIVE_FAULT"],
                "alarms": [],
                "hmi": {"faceplate": "conveyor-v1", "historian": False},
                "requirements": [],
            }
        ],
    }


def test_unknown_controller_reference_fails() -> None:
    payload = _payload()
    payload["equipment"][0]["controller"] = "MISSING"
    with pytest.raises(ControlSpecError, match="unknown controller"):
        parse_control_system_payload(payload)


@pytest.mark.parametrize("standard", ["latest", "conveyor-v2", "conveyor-custom-v999"])
def test_unqualified_standard_version_fails_closed(standard: str) -> None:
    payload = _payload()
    payload["equipment"][0]["standard"] = standard
    with pytest.raises(ControlSpecError, match="not in the V1 CONVEYOR catalog"):
        parse_control_system_payload(payload)


def test_undeclared_permissive_or_interlock_fails() -> None:
    payload = _payload()
    payload["equipment"][0]["interlocks"].append("NOT_DECLARED")
    with pytest.raises(ControlSpecError, match="undeclared signal"):
        parse_control_system_payload(payload)


def test_duplicate_alarm_identity_across_equipment_fails_case_insensitively() -> None:
    payload = _payload()
    first = payload["equipment"][0]
    first["alarms"] = [
        {"id": "ALM_SHARED", "priority": "HIGH", "operator_response": "Inspect fault."}
    ]
    second = copy.deepcopy(first)
    second["id"] = "CONV_102"
    second["alarms"][0]["id"] = "alm_shared"
    payload["equipment"].append(second)

    with pytest.raises(ControlSpecError, match="alarm id alm_shared is duplicated"):
        parse_control_system_payload(payload)


def test_duplicate_controller_identity_fails_case_insensitively() -> None:
    payload = _payload()
    payload["controllers"].append(
        {"id": "plc1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
    )
    with pytest.raises(ControlSpecError, match="case-insensitive duplicate"):
        parse_control_system_payload(payload)


def test_duplicate_signal_identity_fails_case_insensitively() -> None:
    payload = _payload()
    payload["equipment"][0]["signals"].append("safe")
    with pytest.raises(ControlSpecError, match="case-insensitive duplicate"):
        parse_control_system_payload(payload)


def test_duplicate_command_identity_fails_case_insensitively() -> None:
    payload = _payload()
    payload["equipment"][0]["commands"]["start"] = True
    with pytest.raises(ControlSpecError, match="case-insensitive duplicate"):
        parse_control_system_payload(payload)


def test_duplicate_json_keys_fail_before_schema_validation(tmp_path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema":"devagent-controls-spec-v2","schema":"other","project_id":"P",'
        '"controllers":[],"equipment":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ControlSpecError, match="duplicate JSON object key"):
        load_control_system_spec(path)


def test_missing_explicit_engineering_field_fails() -> None:
    payload = _payload()
    del payload["equipment"][0]["hmi"]
    with pytest.raises(ControlSpecError, match="missing required field"):
        parse_control_system_payload(payload)


def test_undeclared_fault_fails_closed() -> None:
    payload = _payload()
    payload["equipment"][0]["faults"].append("NOT_DECLARED")
    with pytest.raises(ControlSpecError, match="undeclared signal"):
        parse_control_system_payload(payload)


def test_fault_that_does_not_inhibit_fails_closed() -> None:
    payload = _payload()
    payload["equipment"][0]["interlocks"] = ["GUARD_OPEN"]
    with pytest.raises(ControlSpecError, match="faults must also be declared as interlocks"):
        parse_control_system_payload(payload)
