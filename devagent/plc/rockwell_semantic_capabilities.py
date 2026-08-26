from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RockwellSemanticKind(str, Enum):
    BOOLEAN_CONDITION = "BOOLEAN_CONDITION"
    CONTINUOUS_BOOLEAN_WRITE = "CONTINUOUS_BOOLEAN_WRITE"
    RETENTIVE_ACTION = "RETENTIVE_ACTION"


@dataclass(frozen=True)
class RockwellInstructionCapability:
    instruction: str
    family: str
    semantic_kind: RockwellSemanticKind
    boolean_path_modeled: bool
    fixed_action_value: bool | None = None
    final_state_provable_from_path_only: bool = False


_CAPABILITIES: dict[str, RockwellInstructionCapability] = {
    "XIC": RockwellInstructionCapability(
        "XIC",
        "BOOLEAN",
        RockwellSemanticKind.BOOLEAN_CONDITION,
        True,
    ),
    "XIO": RockwellInstructionCapability(
        "XIO",
        "BOOLEAN",
        RockwellSemanticKind.BOOLEAN_CONDITION,
        True,
    ),
    "OTE": RockwellInstructionCapability(
        "OTE",
        "BOOLEAN_OUTPUT",
        RockwellSemanticKind.CONTINUOUS_BOOLEAN_WRITE,
        True,
        final_state_provable_from_path_only=True,
    ),
    "OTL": RockwellInstructionCapability(
        "OTL",
        "RETENTIVE_OUTPUT",
        RockwellSemanticKind.RETENTIVE_ACTION,
        True,
        fixed_action_value=True,
        final_state_provable_from_path_only=False,
    ),
    "OTU": RockwellInstructionCapability(
        "OTU",
        "RETENTIVE_OUTPUT",
        RockwellSemanticKind.RETENTIVE_ACTION,
        True,
        fixed_action_value=False,
        final_state_provable_from_path_only=False,
    ),
}


def instruction_capability(name: str) -> RockwellInstructionCapability | None:
    return _CAPABILITIES.get(str(name or "").strip().upper())


def retentive_action_value(name: str) -> bool | None:
    capability = instruction_capability(name)
    if capability is None or capability.semantic_kind is not RockwellSemanticKind.RETENTIVE_ACTION:
        return None
    return capability.fixed_action_value


def boolean_path_instruction_names() -> frozenset[str]:
    return frozenset(
        name
        for name, capability in _CAPABILITIES.items()
        if capability.boolean_path_modeled
    )


def capability_snapshot() -> tuple[RockwellInstructionCapability, ...]:
    return tuple(_CAPABILITIES[name] for name in sorted(_CAPABILITIES))


__all__ = [
    "RockwellInstructionCapability",
    "RockwellSemanticKind",
    "boolean_path_instruction_names",
    "capability_snapshot",
    "instruction_capability",
    "retentive_action_value",
]
