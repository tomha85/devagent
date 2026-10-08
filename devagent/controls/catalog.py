from __future__ import annotations

from types import MappingProxyType

# V1 is deliberately small. A controls specification must pin a standard that
# this release actually knows, rather than merely matching a version-shaped
# string such as "conveyor-v999".
_EQUIPMENT_STANDARDS = {
    "MOTOR": frozenset({"motor-v1"}),
    "VFD": frozenset({"vfd-v1"}),
    "CONVEYOR": frozenset({"conveyor-v1"}),
    "VALVE": frozenset({"valve-v1"}),
}

EQUIPMENT_STANDARDS = MappingProxyType(_EQUIPMENT_STANDARDS)
SUPPORTED_EQUIPMENT_TYPES = frozenset(EQUIPMENT_STANDARDS)


def supported_standards(equipment_type: str) -> frozenset[str]:
    try:
        return EQUIPMENT_STANDARDS[equipment_type]
    except KeyError as exc:
        raise ValueError(f"unsupported equipment type: {equipment_type}") from exc


def standard_is_supported(equipment_type: str, standard: str) -> bool:
    return standard in EQUIPMENT_STANDARDS.get(equipment_type, ())
