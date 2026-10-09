from __future__ import annotations

from typing import Any

from devagent.controls.catalog import STANDARDS
from devagent.controls.ir import ControlsIR, controls_ir_sha256
from devagent.controls.models import ControllerSpec, EquipmentSpec
from devagent.controls.symbols import equipment_symbol_map

IGNITION_STAGING_SCHEMA = "devagent-ignition-staging-v2"
IGNITION_GENERATOR_VERSION = "1.4.0"


def _equipment_instance(
    item: EquipmentSpec,
    controller: ControllerSpec,
) -> dict[str, Any]:
    symbols = equipment_symbol_map(item)
    return {
        "id": item.id,
        "path": f"Equipment/{item.id}",
        "type": item.type,
        "standard": item.standard,
        "controller": item.controller,
        "controller_network": controller.network,
        "area": item.area,
        "safety_zone": item.safety_zone,
        "faceplate": item.hmi.faceplate,
        "historian_enabled": item.hmi.historian,
        "command_model": STANDARDS[item.standard].command_model,
        "status_signal_map": {
            status_member: signal_name
            for status_member, signal_name in STANDARDS[item.standard].status_signal_map
        },
        "permissives": list(item.permissives),
        "interlocks": list(item.interlocks),
        "faults": list(item.faults),
        "plc_tags": {
            "signals": dict(symbols["signals"]),
            "commands": dict(symbols["commands"]),
            "status": dict(symbols["status"]),
            "outputs": dict(symbols["outputs"]),
        },
        "io": [
            {
                "member": mapping.member,
                "direction": mapping.direction,
                "address": mapping.address,
            }
            for mapping in item.io
        ],
    }


def generate_ignition_payloads(ir: ControlsIR) -> dict[str, dict[str, Any]]:
    controllers = {item.id: item for item in ir.controllers}
    instances = [
        _equipment_instance(item, controllers[item.controller])
        for item in ir.equipment
    ]

    definitions = []
    for standard_id in sorted({item.standard for item in ir.equipment}):
        standard = STANDARDS[standard_id]
        definitions.append(
            {
                "id": standard.id,
                "equipment_type": standard.equipment_type,
                "required_commands": list(standard.required_commands),
                "required_status": list(standard.required_status),
                "required_feedback_signals": list(standard.required_feedback_signals),
                "status_signal_map": {
                    status_member: signal_name
                    for status_member, signal_name in standard.status_signal_map
                },
                "command_model": standard.command_model,
                "generated_outputs": list(standard.generated_outputs),
                "default_faceplate": standard.default_faceplate,
                "min_permissives": standard.min_permissives,
                "min_interlocks": standard.min_interlocks,
                "min_faults": standard.min_faults,
                "min_alarms": standard.min_alarms,
                "fault_status_member": standard.fault_status_member,
                "alarm_source_policy": standard.alarm_source_policy,
                "historian_policy": standard.historian_policy,
            }
        )

    alarms: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    navigation: list[dict[str, Any]] = []
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
                    "area": item.area,
                    "description": alarm.description,
                    "priority": alarm.priority,
                    "on_delay_ms": alarm.on_delay_ms,
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
                    "area": item.area,
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
                    "area": item.area,
                    "faceplate": item.hmi.faceplate,
                    "instance_path": f"Equipment/{item.id}",
                }
            )
            navigation.append(
                {
                    "equipment_id": item.id,
                    "area": item.area,
                    "target": f"Equipment/{item.id}",
                    "view": item.hmi.faceplate,
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
            "history": sorted(
                history,
                key=lambda value: (value["equipment_id"], value["member"]),
            ),
        },
        "views.json": {
            **common,
            "views": sorted(views, key=lambda value: value["equipment_id"]),
        },
        "navigation.json": {
            **common,
            "navigation": sorted(
                navigation,
                key=lambda value: (value["area"] or "", value["equipment_id"]),
            ),
        },
    }


def ignition_binding_check(
    ir: ControlsIR,
    payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    known_plc_tags: set[str] = set()
    known_equipment = {item.id for item in ir.equipment}
    for item in ir.equipment:
        symbols = equipment_symbol_map(item)
        for category in ("signals", "commands", "status", "outputs"):
            known_plc_tags.update(
                str(value) for value in dict(symbols[category]).values()
            )

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
    unknown_navigation = [
        item["equipment_id"]
        for item in payloads["navigation.json"]["navigation"]
        if item["equipment_id"] not in known_equipment
    ]
    for instance in payloads["equipment.json"]["instances"]:
        for category in instance["plc_tags"].values():
            for tag in category.values():
                if tag not in known_plc_tags:
                    unknown_instance_tags.append(tag)

    passed = (
        not unbound
        and not unknown_history
        and not unknown_instance_tags
        and not unknown_navigation
    )
    return {
        "schema": "devagent-controls-ignition-binding-v2",
        "status": "PASS" if passed else "FAIL",
        "unbound_alarms": sorted(unbound),
        "unknown_history_tags": sorted(set(unknown_history)),
        "unknown_instance_tags": sorted(set(unknown_instance_tags)),
        "unknown_navigation_equipment": sorted(set(unknown_navigation)),
        "gateway_import_validation": "NOT_RUN",
        "gateway_deployment_performed": False,
    }


def normalize_ignition_projection(projection: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize an Ignition semantic export independent of vendor list ordering."""

    if projection.get("schema") != "devagent-controls-ignition-semantic-projection-v1":
        raise ValueError("unsupported Ignition semantic projection schema")

    def rows(name: str) -> list[dict[str, Any]]:
        value = projection.get(name)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError(f"Ignition semantic projection {name} must be a list of objects")
        return [dict(item) for item in value]

    udts = rows("udt_definitions")
    instances = rows("instances")
    alarms = rows("alarms")
    history = rows("history")
    views = rows("views")
    navigation = rows("navigation")

    return {
        "schema": "devagent-controls-ignition-semantic-projection-v1",
        "udt_definitions": sorted(udts, key=lambda value: str(value.get("id", ""))),
        "instances": sorted(instances, key=lambda value: str(value.get("id", ""))),
        "alarms": sorted(alarms, key=lambda value: str(value.get("id", ""))),
        "history": sorted(
            history,
            key=lambda value: (
                str(value.get("equipment_id", "")),
                str(value.get("member", "")),
                str(value.get("plc_tag", "")),
            ),
        ),
        "views": sorted(
            views,
            key=lambda value: (
                str(value.get("equipment_id", "")),
                str(value.get("faceplate", "")),
            ),
        ),
        "navigation": sorted(
            navigation,
            key=lambda value: (
                str(value.get("area") or ""),
                str(value.get("equipment_id", "")),
                str(value.get("target", "")),
            ),
        ),
    }


def ignition_semantic_projection(
    payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the normalized semantic surface that a Gateway export adapter must prove.

    This intentionally excludes generator bookkeeping fields and contains only the
    engineering meaning that must survive staging import/export.
    """

    required = {
        "udts.json",
        "equipment.json",
        "alarms.json",
        "history.json",
        "views.json",
        "navigation.json",
    }
    missing = sorted(required - set(payloads))
    if missing:
        raise ValueError(
            "Ignition semantic projection is missing staging payload(s): "
            + ", ".join(missing)
        )

    return normalize_ignition_projection(
        {
            "schema": "devagent-controls-ignition-semantic-projection-v1",
            "udt_definitions": payloads["udts.json"]["udt_definitions"],
            "instances": payloads["equipment.json"]["instances"],
            "alarms": payloads["alarms.json"]["alarms"],
            "history": payloads["history.json"]["history"],
            "views": payloads["views.json"]["views"],
            "navigation": payloads["navigation.json"]["navigation"],
        }
    )
