from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from devagent.plc import rockwell_structure as _structure
from devagent.plc.models import PLCSemanticState

_ORIGINAL_AUGMENT = _structure.augment_rockwell_structure
_MAX_L5X_BYTES = 128 * 1024 * 1024


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _major_fault_program(project) -> str | None:
    """Read the controller MajorFaultProgram from the exact authenticated L5X bytes."""

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


def _programs_without_executable_entry(project) -> set[str]:
    scheduled = {
        name.casefold()
        for task in project.tasks
        for name in task.scheduled_programs
    }
    major_fault = getattr(project.metadata, "major_fault_program", None)
    controller_entries = {major_fault.casefold()} if major_fault else set()
    executable_entries = scheduled | controller_entries
    return {
        program.name.casefold()
        for program in project.programs
        if program.routine_ids
        and (
            program.name.casefold() not in executable_entries
            or not program.main_routine_name
        )
    }


def _withhold_unentered_program_semantics(project) -> None:
    blocked = _programs_without_executable_entry(project)
    if not blocked:
        return
    project.output_logic = [
        replace(item, semantic_state=PLCSemanticState.PARTIAL)
        if item.source.program and item.source.program.casefold() in blocked
        else item
        for item in project.output_logic
    ]
    project.logic_statements = [
        replace(item, semantic_state=PLCSemanticState.PARTIAL)
        if item.source.program and item.source.program.casefold() in blocked
        else item
        for item in project.logic_statements
    ]


def augment_rockwell_structure(project) -> None:
    _ORIGINAL_AUGMENT(project)
    project.metadata = replace(
        project.metadata,
        major_fault_program=_major_fault_program(project),
    )
    _withhold_unentered_program_semantics(project)


def install() -> None:
    _structure.augment_rockwell_structure = augment_rockwell_structure


__all__ = ["augment_rockwell_structure", "install"]
