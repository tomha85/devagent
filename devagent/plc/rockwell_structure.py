from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from devagent.plc.models import (
    PLCDataTypeMember,
    PLCDependencyEdge,
    StaticCheck,
    StaticCheckStatus,
)

_MAX_L5X_BYTES = 128 * 1024 * 1024
_WARNING_PREFIX = "Rockwell execution structure: "
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


def _text_child(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    if child is None:
        return None
    value = "".join(child.itertext()).strip()
    return value or None


def _bool(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _verified_root(project) -> ET.Element:
    target = Path(project.metadata.source_path).expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_L5X_BYTES:
        raise ValueError(f"L5X project exceeds {_MAX_L5X_BYTES} bytes during Rockwell structure pass")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != project.metadata.source_sha256:
        raise ValueError(
            "Rockwell L5X changed before execution-structure normalization; mixed-provenance analysis is refused"
        )
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"Rockwell L5X became invalid before structure normalization: {exc}") from exc


def _jsr_target(rung) -> str | None:
    targets: list[str] = []
    for instruction in rung.instructions:
        if instruction.name.upper() != "JSR" or not instruction.arguments:
            continue
        target = instruction.arguments[0].strip()
        if _ROUTINE_NAME.fullmatch(target):
            targets.append(target)
        else:
            targets.append("")
    if len(targets) != 1:
        return None
    return targets[0] or None


def augment_rockwell_structure(project) -> None:
    root = _verified_root(project)
    controller = next((item for item in root.iter() if _local_name(item.tag) == "Controller"), None)
    if controller is None:
        raise ValueError("Rockwell structure pass could not locate Controller")

    datatype_elements: dict[str, ET.Element] = {}
    data_types = _child(controller, "DataTypes")
    if data_types is not None:
        for item in _children(data_types, "DataType"):
            datatype_elements[item.attrib.get("Name", "").strip()] = item
    normalized_types = []
    for data_type in project.data_types:
        element = datatype_elements.get(data_type.name)
        members: list[PLCDataTypeMember] = []
        if element is not None:
            container = _child(element, "Members")
            if container is not None:
                for member in _children(container, "Member"):
                    name = member.attrib.get("Name", "").strip()
                    member_type = member.attrib.get("DataType", "UNKNOWN").strip() or "UNKNOWN"
                    if not name:
                        continue
                    members.append(
                        PLCDataTypeMember(
                            name=name,
                            data_type=member_type,
                            dimension=member.attrib.get("Dimension"),
                            radix=member.attrib.get("Radix"),
                            hidden=_bool(member.attrib.get("Hidden")),
                            description=_text_child(member, "Description"),
                        )
                    )
        normalized_types.append(replace(data_type, members=tuple(members)))
    project.data_types = normalized_types

    task_elements: dict[str, ET.Element] = {}
    tasks = _child(controller, "Tasks")
    if tasks is not None:
        for item in _children(tasks, "Task"):
            task_elements[item.attrib.get("Name", "").strip()] = item
    normalized_tasks = []
    for task in project.tasks:
        element = task_elements.get(task.name)
        scheduled: list[str] = []
        if element is not None:
            container = _child(element, "ScheduledPrograms")
            if container is not None:
                for item in _children(container, "ScheduledProgram"):
                    name = item.attrib.get("Name", "").strip()
                    if name and name not in scheduled:
                        scheduled.append(name)
        normalized_tasks.append(replace(task, scheduled_programs=tuple(scheduled)))
    project.tasks = normalized_tasks

    program_elements: dict[str, ET.Element] = {}
    programs = _child(controller, "Programs")
    if programs is not None:
        for item in _children(programs, "Program"):
            program_elements[item.attrib.get("Name", "").strip()] = item
    normalized_programs = []
    for program in project.programs:
        element = program_elements.get(program.name)
        main = element.attrib.get("MainRoutineName", "").strip() if element is not None else ""
        fault = element.attrib.get("FaultRoutineName", "").strip() if element is not None else ""
        normalized_programs.append(
            replace(
                program,
                main_routine_name=main or None,
                fault_routine_name=fault or None,
            )
        )
    project.programs = normalized_programs

    retained = [warning for warning in project.warnings if not warning.startswith(_WARNING_PREFIX)]
    program_names = {program.name for program in project.programs}
    routine_names_by_program = {
        program.name: {
            routine.name
            for routine in project.routines
            if routine.program == program.name
        }
        for program in project.programs
    }
    for task in project.tasks:
        missing = [name for name in task.scheduled_programs if name not in program_names]
        if missing:
            retained.append(
                _WARNING_PREFIX
                + f"task {task.name} schedules missing program(s): {', '.join(missing)}"
            )
    for program in project.programs:
        known = routine_names_by_program.get(program.name, set())
        if program.main_routine_name and program.main_routine_name not in known:
            retained.append(
                _WARNING_PREFIX
                + f"program {program.name} MainRoutineName={program.main_routine_name} is not present in normalized routines"
            )
        if program.fault_routine_name and program.fault_routine_name not in known:
            retained.append(
                _WARNING_PREFIX
                + f"program {program.name} FaultRoutineName={program.fault_routine_name} is not present in normalized routines"
            )
    for rung in project.rungs:
        jsrs = [instruction for instruction in rung.instructions if instruction.name.upper() == "JSR"]
        for instruction in jsrs:
            raw_target = instruction.arguments[0].strip() if instruction.arguments else ""
            if _ROUTINE_NAME.fullmatch(raw_target) is None:
                retained.append(
                    _WARNING_PREFIX
                    + f"{rung.source.locator} has a JSR target that is not a fixed routine name"
                )
                continue
            if raw_target not in routine_names_by_program.get(rung.program, set()):
                retained.append(
                    _WARNING_PREFIX
                    + f"{rung.source.locator} calls missing routine {raw_target}"
                )
    project.warnings = list(dict.fromkeys(retained))


def add_rockwell_structure_edges(project, graph) -> None:
    seen = {(edge.source, edge.target, edge.kind, edge.evidence_id) for edge in graph.edges}

    def add(source: str, target: str, kind: str, evidence: str) -> None:
        key = (source, target, kind, evidence)
        if key in seen:
            return
        seen.add(key)
        graph.edges.append(PLCDependencyEdge(source, target, kind, evidence))

    program_by_name = {program.name: program for program in project.programs}
    routine_by_key = {(routine.program, routine.name): routine for routine in project.routines}
    for task in project.tasks:
        for index, program_name in enumerate(task.scheduled_programs):
            program = program_by_name.get(program_name)
            if program is not None:
                add(task.id, program.id, "SCHEDULES", f"{task.id}:order:{index}")
    for program in project.programs:
        if program.main_routine_name:
            routine = routine_by_key.get((program.name, program.main_routine_name))
            if routine is not None:
                add(program.id, routine.id, "ENTRYPOINT", program.id)
        if program.fault_routine_name:
            routine = routine_by_key.get((program.name, program.fault_routine_name))
            if routine is not None:
                add(program.id, routine.id, "FAULT_ROUTINE", program.id)
    for rung in project.rungs:
        for instruction in rung.instructions:
            if instruction.name.upper() != "JSR" or not instruction.arguments:
                continue
            target = instruction.arguments[0].strip()
            if _ROUTINE_NAME.fullmatch(target) is None:
                continue
            routine = routine_by_key.get((rung.program, target))
            if routine is not None:
                add(rung.id, routine.id, "CALLS_ROUTINE", rung.id)


def rockwell_structure_check(project) -> StaticCheck:
    warnings = [item for item in project.warnings if item.startswith(_WARNING_PREFIX)]
    scheduled = sum(len(task.scheduled_programs) for task in project.tasks)
    mains = sum(1 for program in project.programs if program.main_routine_name)
    faults = sum(1 for program in project.programs if program.fault_routine_name)
    members = sum(len(data_type.members) for data_type in project.data_types)
    return StaticCheck(
        id="ROCKWELL_EXECUTION_STRUCTURE",
        status=StaticCheckStatus.WARN if warnings else StaticCheckStatus.PASS,
        summary=(
            f"Normalized {scheduled} task→program schedule entries, {mains} main routine assignment(s), "
            f"{faults} fault routine assignment(s), and {members} UDT member definition(s)."
            + (f" {len(warnings)} structure reference warning(s) remain." if warnings else "")
        ),
        evidence=tuple(warnings),
    )


__all__ = [
    "add_rockwell_structure_edges",
    "augment_rockwell_structure",
    "rockwell_structure_check",
]
