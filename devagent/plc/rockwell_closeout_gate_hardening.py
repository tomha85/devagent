from __future__ import annotations

from dataclasses import replace

from devagent.plc import rockwell_closeout as _closeout

_STRUCTURE_WARNING_PREFIX = "Rockwell execution structure: "
_COMPLEX_COMPARE_NAMES = {"LIM", "LIMIT", "MEQ"}
_ORIGINAL_PROFILE = _closeout.rockwell_capability_profile
_ORIGINAL_CHECK = _closeout.rockwell_support_check


def _scheduled_program_names(project) -> set[str]:
    return {
        name.casefold()
        for task in project.tasks
        for name in task.scheduled_programs
    }


def _entry_program_names(project) -> set[str]:
    entries = _scheduled_program_names(project)
    major_fault = getattr(project.metadata, "major_fault_program", None)
    if major_fault:
        entries.add(major_fault.casefold())
    return entries


def _scheduled_programs_without_main(project):
    scheduled = _scheduled_program_names(project)
    return [
        program
        for program in project.programs
        if program.name.casefold() in scheduled and not program.main_routine_name
    ]


def _unscheduled_executable_programs(project):
    """Programs containing exported routines but no task/controller entry path."""

    entries = _entry_program_names(project)
    return [
        program
        for program in project.programs
        if program.routine_ids and program.name.casefold() not in entries
    ]


def _complex_compare_rungs(project):
    return [
        rung
        for rung in project.rungs
        if any(instruction.name.upper() in _COMPLEX_COMPARE_NAMES for instruction in rung.instructions)
    ]


def rockwell_capability_profile(project):
    """Extend the V9 support contract with entrypoint and compare completeness."""

    profile = _ORIGINAL_PROFILE(project)
    structure_warnings = [
        warning
        for warning in project.warnings
        if warning.startswith(_STRUCTURE_WARNING_PREFIX)
    ]
    missing_main = _scheduled_programs_without_main(project)
    unscheduled = _unscheduled_executable_programs(project)
    complex_compare = _complex_compare_rungs(project)
    static_gaps = dict(profile.get("static_gaps") or {})
    static_gaps["execution_structure_warnings"] = len(structure_warnings)
    static_gaps["scheduled_programs_without_main_routine"] = len(missing_main)
    static_gaps["unscheduled_executable_programs"] = len(unscheduled)
    static_gaps["unmodeled_compare_rungs"] = len(complex_compare)
    profile["static_gaps"] = static_gaps
    profile["execution_structure"] = {
        "warnings": len(structure_warnings),
        "details": structure_warnings,
        "scheduled_programs_without_main_routine": [program.name for program in missing_main],
        "unscheduled_executable_programs": [program.name for program in unscheduled],
        "controller_major_fault_program": getattr(project.metadata, "major_fault_program", None),
    }
    profile["typed_compare"] = {
        "unmodeled_complex_rungs": [rung.id for rung in complex_compare],
        "complex_instruction_names": sorted(
            {
                instruction.name.upper()
                for rung in complex_compare
                for instruction in rung.instructions
                if instruction.name.upper() in _COMPLEX_COMPARE_NAMES
            }
        ),
    }
    profile["static_contract"] = (
        "COMPLETE" if not any(static_gaps.values()) else "PARTIAL_FAIL_CLOSED"
    )
    return profile


def rockwell_support_check(project):
    """Keep the support check consistent with the augmented capability profile."""

    check = _ORIGINAL_CHECK(project)
    structure_warnings = tuple(
        warning
        for warning in project.warnings
        if warning.startswith(_STRUCTURE_WARNING_PREFIX)
    )
    missing_main = _scheduled_programs_without_main(project)
    unscheduled = _unscheduled_executable_programs(project)
    complex_compare = _complex_compare_rungs(project)
    if not structure_warnings and not missing_main and not unscheduled and not complex_compare:
        return check

    extra = [*structure_warnings]
    extra.extend(
        f"{_STRUCTURE_WARNING_PREFIX}scheduled program {program.name} has no MainRoutineName"
        for program in missing_main
    )
    extra.extend(
        f"{_STRUCTURE_WARNING_PREFIX}program {program.name} contains routines but has no task/controller execution entry"
        for program in unscheduled
    )
    extra.extend(rung.id for rung in complex_compare)
    evidence = tuple(dict.fromkeys((*check.evidence, *extra)))

    from devagent.plc.models import StaticCheckStatus

    status = check.status
    if getattr(status, "value", str(status)) == "PASS":
        status = StaticCheckStatus.WARN
    summary = check.summary
    if missing_main:
        summary += (
            f" {len(missing_main)} scheduled program(s) have no MainRoutineName; "
            "entry-point execution cannot be statically proven."
        )
    if unscheduled:
        summary += (
            f" {len(unscheduled)} program(s) contain exported routines but have no normalized task/controller entry path; "
            "their execution cannot be included in full-project proof."
        )
    if complex_compare:
        summary += (
            f" {len(complex_compare)} LIM/LIMIT/MEQ rung(s) remain directionally recognized but outside bounded typed threshold proof."
        )
    return replace(check, status=status, summary=summary, evidence=evidence)


def install() -> None:
    _closeout.rockwell_capability_profile = rockwell_capability_profile
    _closeout.rockwell_support_check = rockwell_support_check


__all__ = ["install", "rockwell_capability_profile", "rockwell_support_check"]
