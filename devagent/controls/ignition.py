from __future__ import annotations

from typing import Any

from devagent.controls.catalog import STANDARDS
from devagent.controls.ir import ControlsIR, controls_ir_sha256
from devagent.controls.models import EquipmentSpec
from devagent.controls.symbols import equipment_symbol_map

IGNITION_STAGING_SCHEMA = "devagent-ignition-staging-v1"
IGNITION_GENERATOR_VERSION = "1.0.0"


def _equipment_instance(item: EquipmentSpec) -> dict[str, Any]:
    symbols = equipment_symbol_map(item)
    return {
        "id": item.id,
        "path": f"Equipment/{item.id}",
        "type": item.type,
        "standard": item.standard,
        "controller": item.controller,
        "faceplate": item.hmi.faceplate,
        "historian_enabled": item.hmi.historian,
        "plc_tags": {
            "signals": dict(symbols["signals"]),
            "commands": dict(symbols["commands"]),
            "status": dict(symbols["status"]),
            "outputs": dict(symbols["outputs"]),
        },
    }


def generate_ignition_payloads(ir: ControlsIR) -> dict[str, dict[str, Any]]:
    instances = [_equipment_instance(item) for item in ir.equipment]

    definitions = []
    for standard_id in sorted({item.standard for item in ir.equipment}):
        standard = STANDARDS[standard_id]
        definitions.append(
            {
                "id": standard.id,
                "equipment_type": standard.equipment_type,
                "required_commands": list(standard.required_commands),
                "required_status": list(standard.required_status),
                "generated_outputs": list(standard.generated_outputs),
                "default_faceplate": standard.default_faceplate,
            }
        )

    alarms: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for item in ir.equipment:
        symbols = equipment_symbol_map(item)
        signal_tags = dict(symbols["signals"])
        status_tags = dict(symbols["status"])
        output_tags = dict(symbols["outputs"])
        for alarm in item.alarms:
            source_tag = (
                signal_tags.get(alarm.source_signal)
                if alarm.source_signal is not None
                else None
            )
            alarms.append(
                {
                    "id": alarm.id,
                    "equipment_id": item.id,
                    "priority": alarm.priority,
                    "operator_response": alarm.operator_response,
                    "source_signal": alarm.source_signal,
                    "plc_tag": source_tag,
                    "binding_status": "BOUND" if source_tag is not None else "UNBOUND",
                }
            )
        if item.hmi.historian:
            history.extend(
                {
                    "equipment_id": item.id,
                    "member": member,
                    "plc_tag": tag,
                    "policy": "ON_CHANGE",
                }
                for member, tag in sorted({**status_tags, **output_tags}.items())
            )
        if item.hmi.faceplate is not None:
            views.append(
                {
                    "equipment_id": item.id,
                    "faceplate": item.hmi.faceplate,
                    "instance_path": f"Equipment/{item.id}",
                }
            )

    common = {
        "schema": IGNITION_STAGING_SCHEMA,
        "generator_version": IGNITION_GENERATOR_VERSION,
        "project_id": ir.project_id,
        "controls_ir_sha256": controls_ir_sha256(ir),
        "designer_required_for_standard_generation": False,
        "gateway_deployment_performed": False,
    }
    return {
        "udts.json": {**common, "udt_definitions": definitions},
        "equipment.json": {**common, "instances": instances},
        "alarms.json": {**common, "alarms": sorted(alarms, key=lambda value: value["id"])},
        "history.json": {
            **common,
            "history": sorted(history, key=lambda value: (value["equipment_id"], value["member"])),
        },
        "views.json": {
            **common,
            "views": sorted(views, key=lambda value: value["equipment_id"]),
        },
    }


def ignition_binding_check(ir: ControlsIR, payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    known_plc_tags: set[str] = set()
    for item in ir.equipment:
        symbols = equipment_symbol_map(item)
        for category in ("signals", "commands", "status", "outputs"):
            known_plc_tags.update(str(value) for value in dict(symbols[category]).values())

    unbound = [
        alarm["id"]
        for alarm in payloads["alarms.json"]["alarms"]
        if alarm["binding_status"] != "BOUND"
    ]
    unknown_history = [
        item["plc_tag"]
        for item in payloads["history.json"]["history"]
        if item["plc_tag"] not in known_plc_tags
    ]
    unknown_instance_tags: list[str] = []
    for instance in payloads["equipment.json"]["instances"]:
        for category in instance["plc_tags"].values():
            for tag in category.values():
                if tag not in known_plc_tags:
                    unknown_instance_tags.append(tag)

    passed = not unbound and not unknown_history and not unknown_instance_tags
    return {
        "status": "PASS" if passed else "FAIL",
        "unbound_alarms": sorted(unbound),
        "unknown_history_tags": sorted(set(unknown_history)),
        "unknown_instance_tags": sorted(set(unknown_instance_tags)),
    }
