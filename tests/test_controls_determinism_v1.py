from __future__ import annotations

import copy

from devagent.controls.ir import build_controls_ir, controls_ir_sha256
from devagent.controls.normalize import canonical_json_bytes, normalized_spec_payload, spec_sha256
from devagent.controls.schema import parse_control_system_payload


def _equipment(equipment_id: str, controller: str):
    return {
        "id": equipment_id,
        "type": "VALVE",
        "standard": "valve-v1",
        "controller": controller,
        "signals": ["OPEN_FB", "CLOSED_FB", "PROCESS_OK", "BLOCKED"],
        "commands": {"OPEN": True, "CLOSE": True},
        "status": ["OPEN", "CLOSED"],
        "permissives": ["PROCESS_OK"],
        "interlocks": ["BLOCKED"],
        "faults": ["BLOCKED"],
        "alarms": [
            {
                "id": f"ALM_{equipment_id}_TRAVEL",
                "priority": "MEDIUM",
                "operator_response": "Inspect valve travel feedback.",
                "source_signal": "BLOCKED",
            }
        ],
        "hmi": {"faceplate": "valve-v1", "historian": False},
        "requirements": [
            {
                "id": f"REQ_{equipment_id}_PROCESS",
                "text": f"{equipment_id} may open only when PROCESS_OK is active.",
                "criticality": "MEDIUM",
            }
        ],
    }


def _payload():
    return {
        "schema": "devagent-controls-spec-v2",
        "project_id": "PROCESS01",
        "controllers": [
            {"id": "PLC_B", "vendor": "SIEMENS", "platform": "S7-1500"},
            {"id": "PLC_A", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"},
        ],
        "equipment": [_equipment("VLV_102", "PLC_B"), _equipment("VLV_101", "PLC_A")],
    }


def test_semantically_unordered_input_has_stable_spec_and_ir_hashes() -> None:
    first = _payload()
    second = copy.deepcopy(first)

    second["controllers"].reverse()
    second["equipment"].reverse()
    for item in second["equipment"]:
        item["signals"].reverse()
        item["status"].reverse()
        item["permissives"].reverse()
        item["interlocks"].reverse()
        item["faults"].reverse()
        item["alarms"].reverse()
        item["requirements"].reverse()
        item["commands"] = dict(reversed(list(item["commands"].items())))

    spec_a = parse_control_system_payload(first)
    spec_b = parse_control_system_payload(second)

    assert spec_sha256(spec_a) == spec_sha256(spec_b)
    assert canonical_json_bytes(normalized_spec_payload(spec_a)) == canonical_json_bytes(
        normalized_spec_payload(spec_b)
    )
    assert controls_ir_sha256(build_controls_ir(spec_a)) == controls_ir_sha256(
        build_controls_ir(spec_b)
    )
