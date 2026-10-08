from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from devagent.controls.catalog import (
    SUPPORTED_EQUIPMENT_TYPES,
    standard_is_supported,
    supported_standards,
)
from devagent.controls.models import (
    AlarmSpec,
    ControllerSpec,
    ControlSystemSpec,
    EquipmentSpec,
    HMIContract,
    RequirementSpec,
)

CONTROL_SPEC_SCHEMA = "devagent-controls-spec-v1"

_SUPPORTED_VENDORS = {"ROCKWELL", "SIEMENS", "SCHNEIDER"}
_PRIORITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
_CRITICALITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,95}$")


class ControlSpecError(ValueError):
    """Raised when a controls-authoring document violates the V1 contract."""


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlSpecError(f"{where} must be a JSON object")
    return value


def _fields(
    value: Mapping[str, Any],
    *,
    where: str,
    required: set[str],
    allowed: set[str] | None = None,
) -> None:
    permitted = required if allowed is None else allowed
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - permitted)
    if missing:
        raise ControlSpecError(f"{where} is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ControlSpecError(f"{where} contains unknown field(s): {', '.join(unknown)}")


def _text(value: Any, where: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ControlSpecError(f"{where} must be a string")
    result = value.strip()
    if not result:
        raise ControlSpecError(f"{where} must not be empty")
    if len(result) > maximum:
        raise ControlSpecError(f"{where} exceeds {maximum} characters")
    return result


def _identifier(value: Any, where: str) -> str:
    result = _text(value, where, maximum=64)
    if not _ID_RE.fullmatch(result):
        raise ControlSpecError(
            f"{where} must start with a letter and contain only letters, digits, _, ., :, or -"
        )
    return result


def _symbol(value: Any, where: str) -> str:
    result = _text(value, where, maximum=96)
    if not _SYMBOL_RE.fullmatch(result):
        raise ControlSpecError(
            f"{where} must start with a letter/_ and contain only letters, digits, _, ., :, or -"
        )
    return result


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ControlSpecError(f"{where} must be true or false")
    return value


def _ensure_casefold_unique(values: list[str], where: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        key = value.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ControlSpecError(
                f"{where} contains case-insensitive duplicate: {previous} / {value}"
            )
        seen[key] = value


def _unique(values: list[str], where: str) -> tuple[str, ...]:
    _ensure_casefold_unique(values, where)
    return tuple(values)


def _symbol_list(value: Any, where: str, *, require_nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ControlSpecError(f"{where} must be a JSON array")
    if require_nonempty and not value:
        raise ControlSpecError(f"{where} must contain at least one item")
    return _unique([_symbol(item, f"{where}[{index}]") for index, item in enumerate(value)], where)


def _parse_controller(raw: Any, index: int) -> ControllerSpec:
    where = f"controllers[{index}]"
    item = _object(raw, where)
    required = {"id", "vendor", "platform"}
    _fields(item, where=where, required=required)
    vendor = _text(item["vendor"], f"{where}.vendor", maximum=32).upper()
    if vendor not in _SUPPORTED_VENDORS:
        raise ControlSpecError(
            f"{where}.vendor must be one of: {', '.join(sorted(_SUPPORTED_VENDORS))}"
        )
    return ControllerSpec(
        id=_identifier(item["id"], f"{where}.id"),
        vendor=vendor,
        platform=_text(item["platform"], f"{where}.platform", maximum=96),
    )


def _parse_alarm(raw: Any, where: str) -> AlarmSpec:
    item = _object(raw, where)
    required = {"id", "priority", "operator_response"}
    allowed = {*required, "source_signal"}
    _fields(item, where=where, required=required, allowed=allowed)
    priority = _text(item["priority"], f"{where}.priority", maximum=16).upper()
    if priority not in _PRIORITIES:
        raise ControlSpecError(
            f"{where}.priority must be one of: {', '.join(sorted(_PRIORITIES))}"
        )
    source_signal_raw = item.get("source_signal")
    source_signal = (
        None
        if source_signal_raw is None
        else _symbol(source_signal_raw, f"{where}.source_signal")
    )
    return AlarmSpec(
        id=_identifier(item["id"], f"{where}.id"),
        priority=priority,
        operator_response=_text(
            item["operator_response"], f"{where}.operator_response", maximum=1024
        ),
        source_signal=source_signal,
    )


def _parse_requirement(raw: Any, where: str) -> RequirementSpec:
    item = _object(raw, where)
    required = {"id", "text", "criticality"}
    _fields(item, where=where, required=required)
    criticality = _text(item["criticality"], f"{where}.criticality", maximum=16).upper()
    if criticality not in _CRITICALITIES:
        raise ControlSpecError(
            f"{where}.criticality must be one of: {', '.join(sorted(_CRITICALITIES))}"
        )
    return RequirementSpec(
        id=_identifier(item["id"], f"{where}.id"),
        text=_text(item["text"], f"{where}.text", maximum=4096),
        criticality=criticality,
    )


def _parse_hmi(raw: Any, where: str) -> HMIContract:
    item = _object(raw, where)
    required = {"faceplate", "historian"}
    _fields(item, where=where, required=required)
    faceplate_raw = item["faceplate"]
    faceplate: str | None
    if faceplate_raw is None:
        faceplate = None
    else:
        faceplate = _text(faceplate_raw, f"{where}.faceplate", maximum=128)
    return HMIContract(faceplate=faceplate, historian=_bool(item["historian"], f"{where}.historian"))


def _parse_commands(raw: Any, where: str) -> tuple[tuple[str, bool], ...]:
    item = _object(raw, where)
    if not item:
        raise ControlSpecError(f"{where} must contain at least one command")
    result: list[tuple[str, bool]] = []
    names: list[str] = []
    for name, enabled in item.items():
        symbol = _symbol(name, f"{where} key")
        names.append(symbol)
        result.append((symbol, _bool(enabled, f"{where}.{symbol}")))
    _ensure_casefold_unique(names, where)
    return tuple(result)


def _parse_equipment(raw: Any, index: int) -> EquipmentSpec:
    where = f"equipment[{index}]"
    item = _object(raw, where)
    required = {
        "id", "type", "standard", "controller", "signals", "commands", "status",
        "permissives", "interlocks", "alarms", "hmi", "requirements",
    }
    _fields(item, where=where, required=required)

    equipment_type = _text(item["type"], f"{where}.type", maximum=32).upper()
    if equipment_type not in SUPPORTED_EQUIPMENT_TYPES:
        raise ControlSpecError(
            f"{where}.type must be one of: {', '.join(sorted(SUPPORTED_EQUIPMENT_TYPES))}"
        )

    standard = _text(item["standard"], f"{where}.standard", maximum=96)
    if not standard_is_supported(equipment_type, standard):
        supported = ", ".join(sorted(supported_standards(equipment_type)))
        raise ControlSpecError(
            f"{where}.standard is not in the V1 {equipment_type} catalog; supported: {supported}"
        )

    signals = _symbol_list(item["signals"], f"{where}.signals", require_nonempty=True)
    status = _symbol_list(item["status"], f"{where}.status", require_nonempty=True)
    permissives = _symbol_list(item["permissives"], f"{where}.permissives")
    interlocks = _symbol_list(item["interlocks"], f"{where}.interlocks")
    signal_set = set(signals)
    for category, references in (("permissives", permissives), ("interlocks", interlocks)):
        unknown = sorted(set(references) - signal_set)
        if unknown:
            raise ControlSpecError(
                f"{where}.{category} references undeclared signal(s): {', '.join(unknown)}"
            )

    alarms_raw = item["alarms"]
    if not isinstance(alarms_raw, list):
        raise ControlSpecError(f"{where}.alarms must be a JSON array")
    alarms = tuple(
        _parse_alarm(value, f"{where}.alarms[{alarm_index}]")
        for alarm_index, value in enumerate(alarms_raw)
    )
    _ensure_casefold_unique([alarm.id for alarm in alarms], f"{where}.alarms")
    unknown_alarm_signals = sorted(
        {
            alarm.source_signal
            for alarm in alarms
            if alarm.source_signal is not None and alarm.source_signal not in signal_set
        }
    )
    if unknown_alarm_signals:
        raise ControlSpecError(
            f"{where}.alarms references undeclared source signal(s): "
            + ", ".join(unknown_alarm_signals)
        )

    requirements_raw = item["requirements"]
    if not isinstance(requirements_raw, list):
        raise ControlSpecError(f"{where}.requirements must be a JSON array")
    requirements = tuple(
        _parse_requirement(value, f"{where}.requirements[{requirement_index}]")
        for requirement_index, value in enumerate(requirements_raw)
    )
    _ensure_casefold_unique(
        [requirement.id for requirement in requirements], f"{where}.requirements"
    )

    return EquipmentSpec(
        id=_identifier(item["id"], f"{where}.id"),
        type=equipment_type,
        standard=standard,
        controller=_identifier(item["controller"], f"{where}.controller"),
        signals=signals,
        commands=_parse_commands(item["commands"], f"{where}.commands"),
        status=status,
        permissives=permissives,
        interlocks=interlocks,
        alarms=alarms,
        hmi=_parse_hmi(item["hmi"], f"{where}.hmi"),
        requirements=requirements,
    )


def parse_control_system_payload(payload: Any) -> ControlSystemSpec:
    root = _object(payload, "controls specification")
    required = {"schema", "project_id", "controllers", "equipment"}
    _fields(root, where="controls specification", required=required)

    schema = _text(root["schema"], "controls specification.schema", maximum=64)
    if schema != CONTROL_SPEC_SCHEMA:
        raise ControlSpecError(
            f"unsupported controls specification schema {schema!r}; expected {CONTROL_SPEC_SCHEMA!r}"
        )

    raw_controllers = root["controllers"]
    if not isinstance(raw_controllers, list) or not raw_controllers:
        raise ControlSpecError("controls specification.controllers must be a non-empty JSON array")
    controllers = tuple(_parse_controller(item, index) for index, item in enumerate(raw_controllers))

    controller_ids = [item.id for item in controllers]
    _ensure_casefold_unique(controller_ids, "controls specification.controllers")

    raw_equipment = root["equipment"]
    if not isinstance(raw_equipment, list) or not raw_equipment:
        raise ControlSpecError("controls specification.equipment must be a non-empty JSON array")
    equipment = tuple(_parse_equipment(item, index) for index, item in enumerate(raw_equipment))

    equipment_ids = [item.id for item in equipment]
    _ensure_casefold_unique(equipment_ids, "controls specification.equipment")

    known_controllers = set(controller_ids)
    for item in equipment:
        if item.controller not in known_controllers:
            raise ControlSpecError(
                f"equipment {item.id} references unknown controller {item.controller}"
            )

    alarm_owners: dict[str, str] = {}
    requirement_owners: dict[str, str] = {}
    for item in equipment:
        for alarm in item.alarms:
            previous = alarm_owners.setdefault(alarm.id.casefold(), item.id)
            if previous != item.id:
                raise ControlSpecError(
                    f"alarm id {alarm.id} is duplicated by equipment {previous} and {item.id}"
                )
        for requirement in item.requirements:
            previous = requirement_owners.setdefault(requirement.id.casefold(), item.id)
            if previous != item.id:
                raise ControlSpecError(
                    f"requirement id {requirement.id} is duplicated by equipment {previous} and {item.id}"
                )

    return ControlSystemSpec(
        schema=schema,
        project_id=_identifier(root["project_id"], "controls specification.project_id"),
        controllers=controllers,
        equipment=equipment,
    )
