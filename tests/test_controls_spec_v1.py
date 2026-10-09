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
                        "source_signal": "DRIVE_FAULT",
                    }
                ],
                "hmi": {"faceplate": "conveyor-v1", "historian": True},
                "requirements": [
                    {
                        "id": "REQ_GUARD",
                        "text": "Conveyor must not run with the guard open.",
                        "criticality": "HIGH",
                        "assertion": {
                            "conditions": {
                                "COMMAND.START": True,
                                "SIGNAL.GUARD_OPEN": True,
                            },
                            "expect": {"OUTPUT.RUN": False},
                        },
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


def test_duplicate_equipment_identity_fails_case_insensitively() -> None:
    payload = _payload()
    duplicate = copy.deepcopy(payload["equipment"][0])
    duplicate["id"] = "conv_101"
    duplicate["alarms"] = []
    duplicate["requirements"] = []
    payload["equipment"].append(duplicate)
    with pytest.raises(ControlSpecError, match="case-insensitive duplicate"):
        parse_control_system_payload(payload)


def test_v2_parses_engineering_metadata_io_and_structured_assertion() -> None:
    payload = _payload()
    payload["controllers"][0]["network"] = "PACKAGING_NET"
    equipment = payload["equipment"][0]
    equipment["area"] = "PACKAGING"
    equipment["safety_zone"] = "SZ03"
    equipment["io"] = [
        {
            "member": "SIGNAL.GUARD_OPEN",
            "direction": "INPUT",
            "address": "Local:1:I.Data.0",
        },
        {
            "member": "OUTPUT.RUN",
            "direction": "OUTPUT",
            "address": "Local:2:O.Data.0",
        },
    ]

    result = parse_control_system_payload(payload)

    assert result.controllers[0].network == "PACKAGING_NET"
    assert result.equipment[0].area == "PACKAGING"
    assert result.equipment[0].safety_zone == "SZ03"
    assert result.equipment[0].io[1].member == "OUTPUT.RUN"
    assertion = result.equipment[0].requirements[0].assertion
    assert assertion is not None
    assert assertion.expected_ref == "OUTPUT.RUN"
    assert assertion.expected_value is False


def test_io_direction_must_match_logical_member_kind() -> None:
    payload = _payload()
    payload["equipment"][0]["io"] = [
        {
            "member": "OUTPUT.RUN",
            "direction": "INPUT",
            "address": "Local:1:I.Data.0",
        }
    ]
    with pytest.raises(ControlSpecError, match="INPUT mapping"):
        parse_control_system_payload(payload)


def test_structured_assertion_rejects_unknown_logical_reference() -> None:
    payload = _payload()
    payload["equipment"][0]["requirements"][0]["assertion"]["expect"] = {
        "OUTPUT.NOT_DECLARED": False
    }
    with pytest.raises(ControlSpecError, match="undeclared output"):
        parse_control_system_payload(payload)
