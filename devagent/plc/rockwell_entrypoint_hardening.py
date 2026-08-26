from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from devagent.plc import rockwell_structure as _structure

_ORIGINAL_AUGMENT = _structure.augment_rockwell_structure
_MAX_L5X_BYTES = 128 * 1024 * 1024


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _major_fault_program(project) -> str | None:
    """Read the controller MajorFaultProgram from the exact authenticated L5X bytes.

    The V7 structure pass already re-reads the source with a SHA-256 guard. This
    follow-up read repeats that guard so a controller-level entrypoint can be
    retained without accepting mixed-provenance metadata.
    """

    target = Path(project.metadata.source_path).expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_L5X_BYTES:
        raise ValueError(f"L5X project exceeds {_MAX_L5X_BYTES} bytes during Rockwell entrypoint pass")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != project.metadata.source_sha256:
        raise ValueError(
            "Rockwell L5X changed before controller-entrypoint normalization; mixed-provenance analysis is refused"
        )
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"Rockwell L5X became invalid before controller-entrypoint normalization: {exc}") from exc
    controller = next((item for item in root.iter() if _local_name(item.tag) == "Controller"), None)
    if controller is None:
        raise ValueError("Rockwell entrypoint pass could not locate Controller")
    value = controller.attrib.get("MajorFaultProgram", "").strip()
    return value or None


def augment_rockwell_structure(project) -> None:
    _ORIGINAL_AUGMENT(project)
    project.metadata = replace(
        project.metadata,
        major_fault_program=_major_fault_program(project),
    )


def install() -> None:
    _structure.augment_rockwell_structure = augment_rockwell_structure


__all__ = ["augment_rockwell_structure", "install"]
