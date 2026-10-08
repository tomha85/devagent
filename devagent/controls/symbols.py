from __future__ import annotations

import hashlib
import re

from devagent.controls.catalog import get_standard
from devagent.controls.models import EquipmentSpec

_ROCKWELL_MAX_NAME = 40
_INVALID = re.compile(r"[^A-Za-z0-9_]")


def _safe_fragment(value: str) -> str:
    fragment = _INVALID.sub("_", value.strip())
    fragment = re.sub(r"_+", "_", fragment).strip("_")
    if not fragment:
        fragment = "X"
    if fragment[0].isdigit():
        fragment = "X_" + fragment
    return fragment.upper()


def rockwell_name(value: str, *, maximum: int = _ROCKWELL_MAX_NAME) -> str:
    """Return a deterministic Logix-safe identifier bounded to 40 characters."""

    safe = _safe_fragment(value)
    if len(safe) <= maximum:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8].upper()
    prefix = safe[: maximum - len(digest) - 1].rstrip("_")
    return f"{prefix}_{digest}"


def portable_name(value: str, *, maximum: int = 64) -> str:
    """Return a deterministic cross-platform filename/path component."""

    safe = _safe_fragment(value)
    if len(safe) <= maximum:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8].upper()
    prefix = safe[: maximum - len(digest) - 1].rstrip("_")
    return f"{prefix}_{digest}"


def controller_symbol(controller_id: str) -> str:
    return rockwell_name(controller_id)


def equipment_tag(equipment_id: str, kind: str, member: str) -> str:
    return rockwell_name(f"DA_{equipment_id}_{kind}_{member}")


def equipment_symbol_map(equipment: EquipmentSpec) -> dict[str, object]:
    standard = get_standard(equipment.standard)
    result: dict[str, object] = {
        "equipment_id": equipment.id,
        "controller": equipment.controller,
        "standard": equipment.standard,
        "signals": {
            name: equipment_tag(equipment.id, "SIG", name)
            for name in equipment.signals
        },
        "commands": {
            name: equipment_tag(equipment.id, "CMD", name)
            for name, _enabled in equipment.commands
        },
        "status": {
            name: equipment_tag(equipment.id, "STS", name)
            for name in equipment.status
        },
        "outputs": {
            name: equipment_tag(equipment.id, "OUT", name)
            for name in standard.generated_outputs
        },
        "alarms": {
            alarm.id: (
                equipment_tag(equipment.id, "SIG", alarm.source_signal)
                if alarm.source_signal is not None
                else None
            )
            for alarm in equipment.alarms
        },
    }
    names: list[str] = []
    for category in ("signals", "commands", "status", "outputs"):
        names.extend(str(value) for value in dict(result[category]).values())
    if len(names) != len(set(names)):
        raise ValueError(
            f"Rockwell symbol collision after normalization for equipment {equipment.id}"
        )
    return result
