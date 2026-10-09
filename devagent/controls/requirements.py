from __future__ import annotations

from typing import Any

from devagent.controls.ir import ControlsIR
from devagent.controls.models import EquipmentSpec, RequirementSpec
from devagent.controls.symbols import equipment_symbol_map


def logical_ref_to_plc_tag(item: EquipmentSpec, logical_ref: str) -> str | None:
    kind, member = logical_ref.split(".", 1)
    symbols = equipment_symbol_map(item)
    category = {
        "SIGNAL": "signals",
        "COMMAND": "commands",
        "STATUS": "status",
        "OUTPUT": "outputs",
    }.get(kind)
    if category is None:
        return None
    return str(dict(symbols[category]).get(member)) if member in dict(symbols[category]) else None


def requirement_plc_text(item: EquipmentSpec, requirement: RequirementSpec) -> str:
    assertion = requirement.assertion
    if assertion is None:
        return requirement.text
    expected_tag = logical_ref_to_plc_tag(item, assertion.expected_ref)
    if expected_tag is None:
        # HMI-only assertions are intentionally not rewritten into fake PLC
        # requirements. They remain in the Controls FAT/HMI evidence surface.
        return requirement.text

    conditions: list[str] = []
    for logical_ref, value in assertion.conditions:
        tag = logical_ref_to_plc_tag(item, logical_ref)
        if tag is None:
            return requirement.text
        conditions.append(f"{tag}={'TRUE' if value else 'FALSE'}")
    expected = f"{expected_tag} shall be {'TRUE' if assertion.expected_value else 'FALSE'}"
    return "When " + " and ".join(conditions) + ", " + expected + "."


def requirement_is_plc_verifiable(item: EquipmentSpec, requirement: RequirementSpec) -> bool:
    assertion = requirement.assertion
    if assertion is None:
        return False
    if logical_ref_to_plc_tag(item, assertion.expected_ref) is None:
        return False
    return all(
        logical_ref_to_plc_tag(item, logical_ref) is not None
        for logical_ref, _value in assertion.conditions
    )


def requirements_payload(
    ir: ControlsIR,
    *,
    controller_id: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in ir.equipment:
        if controller_id is not None and item.controller != controller_id:
            continue
        for requirement in item.requirements:
            plc_verifiable = requirement_is_plc_verifiable(item, requirement)
            if controller_id is not None and not plc_verifiable:
                # HMI-only requirements are qualified by the Controls HMI/FAT
                # evidence surface, not misrepresented as PLC requirements.
                continue
            rows.append(
                {
                    "id": requirement.id,
                    "text": requirement_plc_text(item, requirement),
                    "source_text": requirement.text,
                    "criticality": requirement.criticality,
                    "verification_mode": "DYNAMIC",
                    "equipment_id": item.id,
                    "controller_id": item.controller,
                    "domain": "PLC" if plc_verifiable else "HMI_OR_CROSS_DOMAIN",
                    "structured_assertion": requirement.assertion is not None,
                    "plc_verifiable": plc_verifiable,
                }
            )
    rows.sort(key=lambda value: (value["controller_id"], value["equipment_id"], value["id"]))
    return {
        "schema": "devagent-controls-requirements-v2",
        "controller_id": controller_id,
        "requirements": rows,
    }


def hmi_requirement_rows(ir: ControlsIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in ir.equipment:
        for requirement in item.requirements:
            assertion = requirement.assertion
            if assertion is None or not assertion.expected_ref.startswith("ALARM."):
                continue
            rows.append(
                {
                    "id": requirement.id,
                    "equipment_id": item.id,
                    "criticality": requirement.criticality,
                    "text": requirement.text,
                    "conditions": dict(assertion.conditions),
                    "expected_ref": assertion.expected_ref,
                    "expected_value": assertion.expected_value,
                }
            )
    return sorted(rows, key=lambda value: (value["equipment_id"], value["id"]))
