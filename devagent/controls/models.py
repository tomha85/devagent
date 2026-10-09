from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControllerSpec:
    id: str
    vendor: str
    platform: str
    network: str | None = None


@dataclass(frozen=True)
class AlarmSpec:
    id: str
    priority: str
    operator_response: str
    source_signal: str | None = None


@dataclass(frozen=True)
class HMIContract:
    faceplate: str | None
    historian: bool


@dataclass(frozen=True)
class RequirementAssertion:
    conditions: tuple[tuple[str, bool], ...]
    expected_ref: str
    expected_value: bool


@dataclass(frozen=True)
class RequirementSpec:
    id: str
    text: str
    criticality: str
    assertion: RequirementAssertion | None = None


@dataclass(frozen=True)
class IOMappingSpec:
    member: str
    direction: str
    address: str


@dataclass(frozen=True)
class EquipmentSpec:
    id: str
    type: str
    standard: str
    controller: str
    signals: tuple[str, ...]
    commands: tuple[tuple[str, bool], ...]
    status: tuple[str, ...]
    permissives: tuple[str, ...]
    interlocks: tuple[str, ...]
    faults: tuple[str, ...]
    alarms: tuple[AlarmSpec, ...]
    hmi: HMIContract
    requirements: tuple[RequirementSpec, ...]
    area: str | None = None
    safety_zone: str | None = None
    io: tuple[IOMappingSpec, ...] = ()


@dataclass(frozen=True)
class ControlSystemSpec:
    schema: str
    project_id: str
    controllers: tuple[ControllerSpec, ...]
    equipment: tuple[EquipmentSpec, ...]
