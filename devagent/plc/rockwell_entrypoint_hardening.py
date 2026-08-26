from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from devagent.plc import rockwell_structure as _structure
from devagent.plc.models import PLCSemanticState

_ORIGINAL_AUGMENT = _structure.augment_rockwell_structure
_MAX_L5X_BYTES = 128 * 1024 * 1024
_ROUTINE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _bool_attr(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _verified_controller(project) -> ET.Element:
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
    return controller


def _controller_execution_state(project) -> tuple[str | None, set[str], tuple[str, ...]]:
    """Read controller-level entries and active task schedules from authenticated bytes."""

    controller = _verified_controller(project)
    major_fault = controller.attrib.get("MajorFaultProgram", "").strip() or None
    active_programs: set[str] = set()
    inhibited_tasks: list[str] = []
    tasks = _child(controller, "Tasks")
    if tasks is not None:
        for task in _children(tasks, "Task"):
            task_name = task.attrib.get("Name", "").strip()
            if _bool_attr(task.attrib.get("InhibitTask")):
                if task_name and task_name not in inhibited_tasks:
                    inhibited_tasks.append(task_name)
                continue
            scheduled = _child(task, "ScheduledPrograms")
            if scheduled is None:
                continue
            for item in _children(scheduled, "ScheduledProgram"):
                name = item.attrib.get("Name", "").strip()
                if name:
                    active_programs.add(name.casefold())
    return major_fault, active_programs, tuple(inhibited_tasks)


def _entry_program_names(project) -> set[str]:
    cached = getattr(project, "_rockwell_active_task_programs", None)
    if cached is None:
        _, active_programs, _ = _controller_execution_state(project)
        result = set(active_programs)
    else:
        result = {str(name).casefold() for name in cached}
    major_fault = getattr(project.metadata, "major_fault_program", None)
    if major_fault:
        result.add(major_fault.casefold())
    return result


def _routines_by_program(project) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for routine in project.routines:
        result.setdefault(routine.program.casefold(), set()).add(routine.name.casefold())
    return result


def _programs_without_executable_entry(project) -> set[str]:
    executable_entries = _entry_program_names(project)
    routines_by_program = _routines_by_program(project)
    blocked: set[str] = set()
    for program in project.programs:
        if not program.routine_ids:
            continue
        key = program.name.casefold()
        known = routines_by_program.get(key, set())
        has_concrete_main = bool(
            program.main_routine_name
            and program.main_routine_name.casefold() in known
        )
        if key not in executable_entries or not has_concrete_main:
            blocked.add(key)
    return blocked


def _reachable_routines(project) -> set[tuple[str, str]]:
    """Compute the routine closure from concrete task/controller entrypoints."""

    entry_programs = _entry_program_names(project)
    routines_by_program = _routines_by_program(project)
    program_by_key = {program.name.casefold(): program for program in project.programs}
    adjacency: dict[tuple[str, str], set[str]] = {}
    for rung in project.rungs:
        owner = (rung.program.casefold(), rung.routine.casefold())
        targets = adjacency.setdefault(owner, set())
        for instruction in rung.instructions:
            if instruction.name.upper() != "JSR" or not instruction.arguments:
                continue
            target = instruction.arguments[0].strip()
            if _ROUTINE_NAME.fullmatch(target):
                targets.add(target.casefold())

    reachable: set[tuple[str, str]] = set()
    for program_key in entry_programs:
        program = program_by_key.get(program_key)
        known = routines_by_program.get(program_key, set())
        if program is None or not known:
            continue
        roots: list[str] = []
        if program.main_routine_name and program.main_routine_name.casefold() in known:
            roots.append(program.main_routine_name.casefold())
        if program.fault_routine_name and program.fault_routine_name.casefold() in known:
            roots.append(program.fault_routine_name.casefold())
        pending = list(dict.fromkeys(roots))
        while pending:
            routine_key = pending.pop()
            identity = (program_key, routine_key)
            if identity in reachable:
                continue
            reachable.add(identity)
            for target in adjacency.get(identity, set()):
                if target in known and (program_key, target) not in reachable:
                    pending.append(target)
    return reachable


def rung_has_execution_entry(project, rung) -> bool:
    """Return True only when a rung is reachable from a concrete active controller/task entry."""

    if not rung.program or not rung.routine:
        return False
    program_key = rung.program.casefold()
    if program_key in _programs_without_executable_entry(project):
        return False
    return (program_key, rung.routine.casefold()) in _reachable_routines(project)


def _withhold_unreachable_semantics(project) -> None:
    blocked_programs = _programs_without_executable_entry(project)
    entry_programs = _entry_program_names(project)
    reachable = _reachable_routines(project)

    def is_unreachable(item) -> bool:
        if not item.source.program or not item.source.routine:
            return False
        program_key = item.source.program.casefold()
        if program_key in blocked_programs:
            return True
        if program_key in entry_programs:
            return (program_key, item.source.routine.casefold()) not in reachable
        return False

    project.output_logic = [
        replace(item, semantic_state=PLCSemanticState.PARTIAL)
        if is_unreachable(item)
        else item
        for item in project.output_logic
    ]
    project.logic_statements = [
        replace(item, semantic_state=PLCSemanticState.PARTIAL)
        if is_unreachable(item)
        else item
        for item in project.logic_statements
    ]


def augment_rockwell_structure(project) -> None:
    _ORIGINAL_AUGMENT(project)
    major_fault, active_programs, inhibited_tasks = _controller_execution_state(project)
    project.metadata = replace(
        project.metadata,
        major_fault_program=major_fault,
    )
    setattr(project, "_rockwell_active_task_programs", tuple(sorted(active_programs)))
    setattr(project, "_rockwell_inhibited_tasks", inhibited_tasks)
    _withhold_unreachable_semantics(project)


def install() -> None:
    _structure.augment_rockwell_structure = augment_rockwell_structure


__all__ = ["augment_rockwell_structure", "install", "rung_has_execution_entry"]
