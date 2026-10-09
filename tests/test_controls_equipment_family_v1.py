from __future__ import annotations

import json

import pytest

from devagent.controls.build import ControlsBuildError, build_controls_spec, verify_controls_build
from devagent.controls.schema import parse_control_system_payload


def _equipment(kind: str, equipment_id: str, controller: str) -> dict:
    if kind == "MOTOR":
        return {
            "id": equipment_id,
            "type": "MOTOR",
            "standard": "motor-v1",
            "controller": controller,
            "signals": ["SAFETY_OK", "OVERLOAD", "RUN_FB"],
            "commands": {"START": True, "STOP": True, "RESET": True},
            "status": ["READY", "RUNNING", "FAULTED"],
            "permissives": ["SAFETY_OK"],
            "interlocks": ["OVERLOAD"],
            "faults": ["OVERLOAD"],
            "alarms": [
                {
                    "id": f"ALM_{equipment_id}_OVERLOAD",
                    "priority": "HIGH",
                    "description": "Equipment alarm.",
                    "on_delay_ms": 0,
                    "operator_response": "Inspect overload.",
                    "source_signal": "OVERLOAD",
                }
            ],
            "hmi": {"faceplate": "motor-v1", "historian": True},
            "requirements": [],
        }
    if kind == "VFD":
        return {
            "id": equipment_id,
            "type": "VFD",
            "standard": "vfd-v1",
            "controller": controller,
            "signals": ["DRIVE_READY", "DRIVE_FAULT", "RUN_FB"],
            "commands": {"RUN": True, "STOP": True, "RESET": True},
            "status": ["READY", "RUNNING", "FAULTED"],
            "permissives": ["DRIVE_READY"],
            "interlocks": ["DRIVE_FAULT"],
            "faults": ["DRIVE_FAULT"],
            "alarms": [
                {
                    "id": f"ALM_{equipment_id}_FAULT",
                    "priority": "HIGH",
                    "description": "Equipment alarm.",
                    "on_delay_ms": 0,
                    "operator_response": "Inspect drive fault.",
                    "source_signal": "DRIVE_FAULT",
                }
            ],
            "hmi": {"faceplate": "vfd-v1", "historian": True},
            "requirements": [],
        }
    if kind == "CONVEYOR":
        return {
            "id": equipment_id,
            "type": "CONVEYOR",
            "standard": "conveyor-v1",
            "controller": controller,
            "signals": ["SAFETY_OK", "DOWNSTREAM_READY", "GUARD_OPEN", "DRIVE_FAULT", "RUN_FB"],
            "commands": {"START": True, "STOP": True, "RESET": True},
            "status": ["READY", "RUNNING", "FAULTED"],
            "permissives": ["SAFETY_OK", "DOWNSTREAM_READY"],
            "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
            "faults": ["DRIVE_FAULT"],
            "alarms": [
                {
                    "id": f"ALM_{equipment_id}_FAULT",
                    "priority": "HIGH",
                    "description": "Equipment alarm.",
                    "on_delay_ms": 0,
                    "operator_response": "Inspect conveyor fault.",
                    "source_signal": "DRIVE_FAULT",
                }
            ],
            "hmi": {"faceplate": "conveyor-v1", "historian": True},
            "requirements": [],
        }
    if kind == "VALVE":
        return {
            "id": equipment_id,
            "type": "VALVE",
            "standard": "valve-v1",
            "controller": controller,
            "signals": ["PROCESS_OK", "BLOCKED", "OPEN_FB", "CLOSED_FB"],
            "commands": {"OPEN": True, "CLOSE": True},
            "status": ["OPEN", "CLOSED"],
            "permissives": ["PROCESS_OK"],
            "interlocks": ["BLOCKED"],
            "faults": ["BLOCKED"],
            "alarms": [
                {
                    "id": f"ALM_{equipment_id}_BLOCKED",
                    "priority": "MEDIUM",
                    "description": "Equipment alarm.",
                    "on_delay_ms": 0,
                    "operator_response": "Inspect valve obstruction.",
                    "source_signal": "BLOCKED",
                }
            ],
            "hmi": {"faceplate": "valve-v1", "historian": True},
            "requirements": [],
        }
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["MOTOR", "VFD", "CONVEYOR", "VALVE"])
def test_each_v1_equipment_family_builds_and_roundtrips(kind: str, tmp_path) -> None:
    payload = {
        "schema": "devagent-controls-spec-v2",
        "project_id": f"{kind}_PROJECT",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
        ],
        "equipment": [_equipment(kind, f"{kind}_101", "PLC1")],
    }
    spec = parse_control_system_payload(payload)
    output = tmp_path / kind.lower()

    result = build_controls_spec(spec, output)
    verification = verify_controls_build(output)

    assert result.status == "PASS"
    assert verification["status"] == "PASS"
    roundtrip = json.loads(
        (output / "verification" / "rockwell-roundtrip.json").read_text(encoding="utf-8")
    )
    assert roundtrip["status"] == "PASS"


def test_multi_controller_project_builds_independent_verified_artifacts(tmp_path) -> None:
    payload = {
        "schema": "devagent-controls-spec-v2",
        "project_id": "MULTI_CONTROLLER",
        "controllers": [
            {"id": "PLC_A", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"},
            {"id": "PLC_B", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"},
        ],
        "equipment": [
            _equipment("CONVEYOR", "CONV_A", "PLC_A"),
            _equipment("MOTOR", "MTR_B", "PLC_B"),
        ],
    }
    output = tmp_path / "multi"
    build_controls_spec(parse_control_system_payload(payload), output)

    assert (output / "rockwell" / "PLC_A.L5X").is_file()
    assert (output / "rockwell" / "PLC_B.L5X").is_file()
    assert verify_controls_build(output)["status"] == "PASS"


def test_unqualified_vendor_generation_fails_closed(tmp_path) -> None:
    payload = {
        "schema": "devagent-controls-spec-v2",
        "project_id": "SIEMENS_FUTURE",
        "controllers": [
            {"id": "PLC1", "vendor": "SIEMENS", "platform": "S7-1500"}
        ],
        "equipment": [_equipment("MOTOR", "MTR_101", "PLC1")],
    }
    spec = parse_control_system_payload(payload)

    with pytest.raises(ControlsBuildError, match="qualified for Rockwell controllers only"):
        build_controls_spec(spec, tmp_path / "unsupported")


def test_compactlogix_generation_fails_until_golden_template_is_qualified(tmp_path) -> None:
    payload = {
        "schema": "devagent-controls-spec-v2",
        "project_id": "COMPACTLOGIX_FUTURE",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "COMPACTLOGIX"}
        ],
        "equipment": [_equipment("MOTOR", "MTR_101", "PLC1")],
    }
    spec = parse_control_system_payload(payload)

    with pytest.raises(ControlsBuildError, match="currently qualifies CONTROLLOGIX only"):
        build_controls_spec(spec, tmp_path / "compactlogix-unqualified")
