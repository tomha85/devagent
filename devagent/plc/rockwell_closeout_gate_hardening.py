from __future__ import annotations

from dataclasses import replace

from devagent.plc import rockwell_closeout as _closeout

_STRUCTURE_WARNING_PREFIX = "Rockwell execution structure: "
_ORIGINAL_PROFILE = _closeout.rockwell_capability_profile
_ORIGINAL_CHECK = _closeout.rockwell_support_check


def rockwell_capability_profile(project):
    """Extend the V9 support contract with execution-structure completeness."""

    profile = _ORIGINAL_PROFILE(project)
    structure_warnings = [
        warning
        for warning in project.warnings
        if warning.startswith(_STRUCTURE_WARNING_PREFIX)
    ]
    static_gaps = dict(profile.get("static_gaps") or {})
    static_gaps["execution_structure_warnings"] = len(structure_warnings)
    profile["static_gaps"] = static_gaps
    profile["execution_structure"] = {
        "warnings": len(structure_warnings),
        "details": structure_warnings,
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
    if not structure_warnings:
        return check
    evidence = tuple(dict.fromkeys((*check.evidence, *structure_warnings)))
    return replace(check, evidence=evidence)


def install() -> None:
    _closeout.rockwell_capability_profile = rockwell_capability_profile
    _closeout.rockwell_support_check = rockwell_support_check


__all__ = ["install", "rockwell_capability_profile", "rockwell_support_check"]
