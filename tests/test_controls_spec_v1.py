from __future__ import annotations

import copy

import pytest

from devagent.controls.schema import (
    CONTROL_SPEC_SCHEMA,
    ControlSpecError,
    parse_control_system_payload,
)


def _payload():
    return {
        "schema": CONTROL_SPEC_SCHEMA,
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
                "signals": ["SAFE", "DOWNSTREAM_READY", "GUARD_OPEN", "DRIVE_FAULT"],
                "commands": {"START": True, "STOP": True, "RESET": True},
                "status": ["READY", "RUNNING", "FAULTED"],
                "permissives": ["SAFE", "DOWNSTREAM_READY"],
                "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                "alarms": [
                    {
                        "id": "ALM_DRIVE_FAULT",
                        "priority": "HIGH",
                        "operator_response": "Inspect drive fault before reset.",
                    }
                ],
                "hmi": {"faceplate": "conveyor-v1", "historian": True},
                "requirements": [
                    {
                        "id": "REQ_GUARD",
                        "text": "Conveyor must not run with the guard open.",
                        "criticality": "HIGH",
                    }
                ],
            }
        ],
    }


def test_v1_spec_parses_explicit_engineering_contract() -> None:
    result = parse_control_system_payload(_payload())
    assert result.project_id == "PACK01"
    assert result.controllers[0].vendor == "ROCKWELL"
    assert result.equipment[0].standard == "conveyor-v1"
    assert result.equipment[0].hmi.historian is True


def test_unknown_fields_fail_closed() -> None:
    payload = _payload()
    payload["surprise"] = True
    with pytest.raises(ControlSpecError, match="unknown field"):
        parse_control_system_payload(payload)


def test_unknown_schema_fails_closed() -> None:
    payload = _payload()
    payload["schema"] = "devagent-controls-spec-v99"
    with pytest.raises(ControlSpecError, match="unsupported controls specification schema"):
        parse_control_system_payload(payload)


def test_duplicate_equipment_identity_fails() -> None:
    payload = _payload()
    payload["equipment"].append(copy.deepcopy(payload["equipment"][0]))
    with pytest.raises(ControlSpecError, match="duplicate equipment ids"):
        parse_control_system_payload(payload)
