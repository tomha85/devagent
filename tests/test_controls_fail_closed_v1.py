from __future__ import annotations

import pytest

from devagent.controls.parser import load_control_system_spec
from devagent.controls.schema import ControlSpecError, parse_control_system_payload


def _payload():
    return {
        "schema": "devagent-controls-spec-v1",
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
                "signals": ["SAFE", "GUARD_OPEN"],
                "commands": {"START": True},
                "status": ["RUNNING"],
                "permissives": ["SAFE"],
                "interlocks": ["GUARD_OPEN"],
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


def test_invalid_standard_version_fails() -> None:
    payload = _payload()
    payload["equipment"][0]["standard"] = "latest"
    with pytest.raises(ControlSpecError, match="explicit conveyor.*-vN"):
        parse_control_system_payload(payload)


def test_undeclared_permissive_or_interlock_fails() -> None:
    payload = _payload()
    payload["equipment"][0]["interlocks"].append("NOT_DECLARED")
    with pytest.raises(ControlSpecError, match="undeclared signal"):
        parse_control_system_payload(payload)


def test_duplicate_alarm_identity_across_equipment_fails() -> None:
    payload = _payload()
    first = payload["equipment"][0]
    first["alarms"] = [
        {"id": "ALM_SHARED", "priority": "HIGH", "operator_response": "Inspect fault."}
    ]
    second = {
        **first,
        "id": "CONV_102",
        "signals": list(first["signals"]),
        "commands": dict(first["commands"]),
        "status": list(first["status"]),
        "permissives": list(first["permissives"]),
        "interlocks": list(first["interlocks"]),
        "alarms": [dict(first["alarms"][0])],
        "hmi": dict(first["hmi"]),
        "requirements": [],
    }
    payload["equipment"].append(second)

    with pytest.raises(ControlSpecError, match="alarm id ALM_SHARED is duplicated"):
        parse_control_system_payload(payload)


def test_duplicate_json_keys_fail_before_schema_validation(tmp_path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema":"devagent-controls-spec-v1","schema":"other","project_id":"P",'
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
