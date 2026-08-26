from __future__ import annotations

from dataclasses import replace

from devagent.plc import rockwell_closeout as _closeout

_STRUCTURE_WARNING_PREFIX = "Rockwell execution structure: "
_ORIGINAL_PROFILE = _closeout.rockwell_capability_profile
_ORIGINAL_CHECK = _closeout.rockwell_support_check


def _scheduled_programs_without_main(project):
    scheduled = {
        name.casefold()
        for task in project.tasks
        for name in task.scheduled_programs
    }
    return [
        program
        for program in project.programs
        if program.name.casefold() in scheduled and not program.main_routine_name
    ]


def rockwell_capability_profile(project):
    """Extend the V9 support contract with execution-structure completeness."""

    profile = _ORIGINAL_PROFILE(project)
    structure_warnings = [
        warning
        for warning in project.warnings
        if warning.startswith(_STRUCTURE_WARNING_PREFIX)
    ]
    missing_main = _scheduled_programs_without_main(project)
    static_gaps = dict(profile.get("static_gaps") or {})
    static_gaps["execution_structure_warnings"] = len(structure_warnings)
    static_gaps["scheduled_programs_without_main_routine"] = len(missing_main)
    profile["static_gaps"] = static_gaps
    profile["execution_structure"] = {
        "warnings": len(structure_warnings),
        "details": structure_warnings,
        "scheduled_programs_without_main_routine": [program.name for program in missing_main],
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
    if not structure_warnings and not missing_main:
        return check
    extra = [*structure_warnings]
    extra.extend(
        f"{_STRUCTURE_WARNING_PREFIX}scheduled program {program.name} has no MainRoutineName"
        for program in missing_main
    )
    evidence = tuple(dict.fromkeys((*check.evidence, *extra)))
    status = check.status
    if missing_main and getattr(status, "value", str(status)) == "PASS":
        from devagent.plc.models import StaticCheckStatus
        status = StaticCheckStatus.WARN
    summary = check.summary
    if missing_main:
        summary += (
            f" {len(missing_main)} scheduled program(s) have no MainRoutineName; "
            "entry-point execution cannot be statically proven."
        )
    return replace(check, status=status, summary=summary, evidence=evidence)


def install() -> None:
    _closeout.rockwell_capability_profile = rockwell_capability_profile
    _closeout.rockwell_support_check = rockwell_support_check


__all__ = ["install", "rockwell_capability_profile", "rockwell_support_check"]
