from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class EquipmentStandard:
    id: str
    equipment_type: str
    required_commands: tuple[str, ...]
    required_status: tuple[str, ...]
    required_feedback_signals: tuple[str, ...]
    status_signal_map: tuple[tuple[str, str], ...]
    command_model: str
    primary_action: str
    stop_action: str | None
    generated_outputs: tuple[str, ...]
    default_faceplate: str
    min_permissives: int
    min_interlocks: int
    min_faults: int
    min_alarms: int
    fault_status_member: str | None
    alarm_source_policy: str
    historian_policy: str


_STANDARDS = {
    "motor-v1": EquipmentStandard(
        id="motor-v1",
        equipment_type="MOTOR",
        required_commands=("START", "STOP", "RESET"),
        required_status=("READY", "RUNNING", "FAULTED"),
        required_feedback_signals=("RUN_FB",),
        status_signal_map=(("RUNNING", "RUN_FB"),),
        command_model="SEAL_IN_PRIMARY_STOP_DOMINANT",
        primary_action="START",
        stop_action="STOP",
        generated_outputs=("RUN", "RESET"),
        default_faceplate="motor-v1",
        min_permissives=1,
        min_interlocks=1,
        min_faults=1,
        min_alarms=1,
        fault_status_member="FAULTED",
        alarm_source_policy="DECLARED_SIGNAL",
        historian_policy="OPTIONAL",
    ),
    "vfd-v1": EquipmentStandard(
        id="vfd-v1",
        equipment_type="VFD",
        required_commands=("RUN", "STOP", "RESET"),
        required_status=("READY", "RUNNING", "FAULTED"),
        required_feedback_signals=("RUN_FB",),
        status_signal_map=(("RUNNING", "RUN_FB"),),
        command_model="SEAL_IN_PRIMARY_STOP_DOMINANT",
        primary_action="RUN",
        stop_action="STOP",
        generated_outputs=("RUN", "RESET"),
        default_faceplate="vfd-v1",
        min_permissives=1,
        min_interlocks=1,
        min_faults=1,
        min_alarms=1,
        fault_status_member="FAULTED",
        alarm_source_policy="DECLARED_SIGNAL",
        historian_policy="OPTIONAL",
    ),
    "conveyor-v1": EquipmentStandard(
        id="conveyor-v1",
        equipment_type="CONVEYOR",
        required_commands=("START", "STOP", "RESET"),
        required_status=("READY", "RUNNING", "FAULTED"),
        required_feedback_signals=("RUN_FB",),
        status_signal_map=(("RUNNING", "RUN_FB"),),
        command_model="SEAL_IN_PRIMARY_STOP_DOMINANT",
        primary_action="START",
        stop_action="STOP",
        generated_outputs=("RUN", "RESET"),
        default_faceplate="conveyor-v1",
        min_permissives=1,
        min_interlocks=1,
        min_faults=1,
        min_alarms=1,
        fault_status_member="FAULTED",
        alarm_source_policy="DECLARED_SIGNAL",
        historian_policy="OPTIONAL",
    ),
    "valve-v1": EquipmentStandard(
        id="valve-v1",
        equipment_type="VALVE",
        required_commands=("OPEN", "CLOSE"),
        required_status=("OPEN", "CLOSED"),
        required_feedback_signals=("OPEN_FB", "CLOSED_FB"),
        status_signal_map=(("OPEN", "OPEN_FB"), ("CLOSED", "CLOSED_FB")),
        command_model="MUTUALLY_EXCLUSIVE_LEVEL",
        primary_action="OPEN",
        stop_action="CLOSE",
        generated_outputs=("OPEN", "CLOSE"),
        default_faceplate="valve-v1",
        min_permissives=1,
        min_interlocks=1,
        min_faults=1,
        min_alarms=1,
        fault_status_member=None,
        alarm_source_policy="DECLARED_SIGNAL",
        historian_policy="OPTIONAL",
    ),
}

STANDARDS = MappingProxyType(_STANDARDS)
SUPPORTED_EQUIPMENT_TYPES = frozenset(item.equipment_type for item in STANDARDS.values())
EQUIPMENT_STANDARDS = MappingProxyType(
    {
        equipment_type: frozenset(
            standard.id
            for standard in STANDARDS.values()
            if standard.equipment_type == equipment_type
        )
        for equipment_type in SUPPORTED_EQUIPMENT_TYPES
    }
)


def get_standard(standard_id: str) -> EquipmentStandard:
    try:
        return STANDARDS[standard_id]
    except KeyError as exc:
        raise ValueError(f"unsupported equipment standard: {standard_id}") from exc


def supported_standards(equipment_type: str) -> frozenset[str]:
    try:
        return EQUIPMENT_STANDARDS[equipment_type]
    except KeyError as exc:
        raise ValueError(f"unsupported equipment type: {equipment_type}") from exc


def standard_is_supported(equipment_type: str, standard: str) -> bool:
    return standard in EQUIPMENT_STANDARDS.get(equipment_type, ())
