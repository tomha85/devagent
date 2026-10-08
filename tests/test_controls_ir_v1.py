from __future__ import annotations

from devagent.controls.ir import build_controls_ir, controls_ir_payload, controls_ir_sha256
from devagent.controls.schema import parse_control_system_payload


def _payload():
    return {
        "schema": "devagent-controls-spec-v1",
        "project_id": "CELL01",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "COMPACTLOGIX"}
        ],
        "equipment": [
            {
                "id": "MTR_101",
                "type": "MOTOR",
                "standard": "motor-v1",
                "controller": "PLC1",
                "signals": ["OVERLOAD", "SAFETY_OK"],
                "commands": {"START": True, "STOP": True},
                "status": ["RUNNING", "FAULTED"],
                "permissives": ["SAFETY_OK"],
                "interlocks": ["OVERLOAD"],
                "alarms": [
                    {
                        "id": "ALM_MTR101_OVERLOAD",
                        "priority": "HIGH",
                        "operator_response": "Inspect motor overload before restart.",
                    }
                ],
                "hmi": {"faceplate": "motor-v1", "historian": False},
                "requirements": [
                    {
                        "id": "REQ_MTR101_OVERLOAD",
                        "text": "MTR_101 must stop on overload.",
                        "criticality": "HIGH",
                    }
                ],
            }
        ],
    }


def test_ir_contains_explicit_spec_identity_and_authoring_intent() -> None:
    spec = parse_control_system_payload(_payload())
    ir = build_controls_ir(spec)
    payload = controls_ir_payload(ir)

    assert payload["schema"] == "devagent-controls-ir-v1"
    assert payload["spec_sha256"] == ir.spec_sha256
    assert payload["equipment"][0]["id"] == "MTR_101"
    assert len(controls_ir_sha256(ir)) == 64


def test_engineering_change_changes_ir_identity() -> None:
    first = _payload()
    second = _payload()
    second["equipment"][0]["hmi"]["historian"] = True

    hash_a = controls_ir_sha256(build_controls_ir(parse_control_system_payload(first)))
    hash_b = controls_ir_sha256(build_controls_ir(parse_control_system_payload(second)))

    assert hash_a != hash_b
