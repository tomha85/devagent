from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class EquipmentStandard:
    equipment_type: str
    standard: str
    required_commands: tuple[str, ...]
    required_status: tuple[str, ...]
    default_faceplate: str
    requires_requirement: bool = True


_STANDARDS = {
    "MOTOR": {
        "motor-v1": EquipmentStandard(
            equipment_type="MOTOR",
            standard="motor-v1",
            required_commands=("START", "STOP", "RESET"),
            required_status=("READY", "RUNNING", "FAULTED"),
            default_faceplate="motor-v1",
        ),
    },
    "VFD": {
        "vfd-v1": EquipmentStandard(
            equipment_type="VFD",
            standard="vfd-v1",
            required_commands=("RUN", "RESET"),
            required_status=("READY", "RUNNING", "FAULTED"),
            default_faceplate="vfd-v1",
        ),
    },
    "CONVEYOR": {
        "conveyor-v1": EquipmentStandard(
            equipment_type="CONVEYOR",
            standard="conveyor-v1",
            required_commands=("START", "STOP", "RESET"),
            required_status=("READY", "RUNNING", "FAULTED"),
            default_faceplate="conveyor-v1",
        ),
    },
    "VALVE": {
        "valve-v1": EquipmentStandard(
            equipment_type="VALVE",
            standard="valve-v1",
            required_commands=("OPEN", "CLOSE"),
            required_status=("OPEN", "CLOSED"),
            default_faceplate="valve-v1",
        ),
    },
}

EQUIPMENT_STANDARDS = MappingProxyType(
    {key: MappingProxyType(value) for key, value in _STANDARDS.items()}
)
SUPPORTED_EQUIPMENT_TYPES = frozenset(EQUIPMENT_STANDARDS)


def supported_standards(equipment_type: str) -> frozenset[str]:
    try:
        return frozenset(EQUIPMENT_STANDARDS[equipment_type])
    except KeyError as exc:
        raise ValueError(f"unsupported equipment type: {equipment_type}") from exc


def standard_is_supported(equipment_type: str, standard: str) -> bool:
    return standard in EQUIPMENT_STANDARDS.get(equipment_type, {})


def get_standard(equipment_type: str, standard: str) -> EquipmentStandard:
    try:
        return EQUIPMENT_STANDARDS[equipment_type][standard]
    except KeyError as exc:
        raise ValueError(
            f"unsupported controls standard {equipment_type}/{standard}"
        ) from exc
