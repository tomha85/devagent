from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from devagent.plc.models import StaticCheck, StaticCheckStatus
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    identity_is_resolved,
)

_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "
_V2_PARTIAL_WARNING_PREFIX = "Recognized but directionally partial Rockwell motion instructions: "
_CLOSEOUT_WARNING_PREFIX = "Rockwell V9 support contract: "
_SUBSCRIPT = re.compile(r"\[([^\]]+)\]")

_PARTIAL_INSTRUCTION_FAMILIES: dict[str, frozenset[str]] = {
    "MOTION": frozenset(
        {
            "MAH", "MAJ", "MAM", "MAS", "MAG", "MCD", "MCLM", "MCS",
            "MCT", "MCTO", "MCCM", "MCPM", "MDAC", "MDCC", "MDO", "MDR",
            "MDS", "MGS", "MGSP", "MRP", "MSF", "MSO", "MASD", "MAFR",
        }
    ),
    "FILE_ARRAY": frozenset(
        {
            "FAL", "FBC", "FSC", "FFL", "FFU", "LFL", "LFU", "FLL",
            "DDT", "BTD", "BSL", "BSR", "SWPB",
        }
    ),
    "SEQUENCER": frozenset({"SQI", "SQO", "SQL"}),
    "COMMUNICATION": frozenset({"MSG"}),
    "PROCESS_CONTROL": frozenset({"PID", "PIDE"}),
    "PROGRAM_CONTROL": frozenset({"MCR", "JMP", "LBL"}),
    "ALARM": frozenset({"ALMA", "ALMD"}),
}

_INSTRUCTION_TO_FAMILY = {
    instruction: family
    for family, instructions in _PARTIAL_INSTRUCTION_FAMILIES.items()
    for instruction in instructions
}
_SUPPORTED_ROUTINE_TYPES = frozenset({"RLL", "ST"})


def _program_from_scope(scope: str) -> str | None:
    prefix = "program:"
    return scope[len(prefix) :] if scope.casefold().startswith(prefix) else None


def instruction_family(name: str) -> str | None:
    return _INSTRUCTION_TO_FAMILY.get(name.upper())


def _has_variable_subscript(value: str) -> bool:
    for match in _SUBSCRIPT.finditer(value):
        expression = match.group(1).strip()
        if not re.fullmatch(r"[-+]?\d+", expression):
            return True
    return False


def _indirect_rungs(project) -> list[str]:
    return [
        rung.id
        for rung in project.rungs
        if any(
            _has_variable_subscript(argument)
            for instruction in rung.instructions
            for argument in instruction.arguments
        )
    ]


def _has_supported_logic(project) -> bool:
    instruction_count = sum(len(rung.instructions) for rung in project.rungs)
    return not (
        instruction_count == 0
        and project.st_statement_total == 0
        and not any(aoi.internal_body_modeled for aoi in project.aois)
    )


def augment_closeout_semantics(project) -> None:
    """Classify known complex Rockwell instructions without overclaiming them."""
    recognized = sorted(
        {
            name
            for name in project.unknown_instruction_names
            if instruction_family(name) is not None
        },
        key=str.casefold,
    )
    if recognized:
        folded = {name.casefold() for name in recognized}
        project.unknown_instruction_names = [
            name
            for name in project.unknown_instruction_names
            if name.casefold() not in folded
        ]
        project.partially_modeled_instruction_names = sorted(
            set(project.partially_modeled_instruction_names) | set(recognized),
            key=str.casefold,
        )

    retained = [
        warning
        for warning in project.warnings
        if not warning.startswith(_UNKNOWN_WARNING_PREFIX)
        and not warning.startswith(_V2_PARTIAL_WARNING_PREFIX)
        and not warning.startswith(_CLOSEOUT_WARNING_PREFIX)
    ]
    if project.unknown_instruction_names:
        retained.append(
            _UNKNOWN_WARNING_PREFIX + ", ".join(sorted(project.unknown_instruction_names, key=str.casefold))
        )

    grouped: dict[str, list[str]] = defaultdict(list)
    for name in project.partially_modeled_instruction_names:
        grouped[instruction_family(name) or "OTHER"].append(name)
    for family in sorted(grouped):
        retained.append(
            _CLOSEOUT_WARNING_PREFIX
            + f"{family} instruction(s) recognized as PARTIAL: "
            + ", ".join(sorted(grouped[family], key=str.casefold))
        )
    project.warnings = list(dict.fromkeys(retained))


def _alias_health(project) -> tuple[list[str], list[str], list[str]]:
    resolved: list[str] = []
    dangling: list[str] = []
    cycles: list[str] = []
    for tag in project.tags:
        if not (tag.alias_for or "").strip():
            continue
        program = _program_from_scope(tag.scope)
        identity = canonical_tag_identity(project, tag.name, program)
        if identity[0] == "alias-cycle":
            cycles.append(tag.id)
        elif not identity_is_resolved(identity):
            dangling.append(tag.id)
        else:
            resolved.append(tag.id)
    return resolved, dangling, cycles


def _instruction_family_profile(project) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in project.partially_modeled_instruction_names:
        grouped[instruction_family(name) or "OTHER"].append(name)
    return {
        family: sorted(names, key=str.casefold)
        for family, names in sorted(grouped.items())
    }


def rockwell_capability_profile(project) -> dict[str, Any]:
    routine_counts = Counter(routine.routine_type.upper() for routine in project.routines)
    unsupported_routines = [
        routine
        for routine in project.routines
        if routine.routine_type.upper() not in _SUPPORTED_ROUTINE_TYPES
    ]
    protected_routines = [routine for routine in project.routines if routine.source_protected]
    protected_aois = [aoi for aoi in project.aois if aoi.source_protected]
    resolved_aliases, dangling_aliases, alias_cycles = _alias_health(project)
    indirect_rungs = _indirect_rungs(project)
    no_supported_logic = not _has_supported_logic(project)

    static_gaps = {
        "unsupported_routines": len(unsupported_routines),
        "protected_routines": len(protected_routines),
        "protected_aois": len(protected_aois),
        "unknown_instructions": len(project.unknown_instruction_names),
        "partial_instructions": len(project.partially_modeled_instruction_names),
        "unmodeled_branches": max(0, project.branch_rung_total - project.branch_rung_semantic_count),
        "unmodeled_st_statements": max(0, project.st_statement_total - project.st_statement_semantic_count),
        "unmodeled_aoi_bodies": max(0, project.aoi_internal_total - project.aoi_internal_modeled_count),
        "unbound_aoi_calls": max(0, project.aoi_call_total - project.aoi_call_bound_count),
        "indirect_rungs": len(indirect_rungs),
        "no_supported_logic": int(no_supported_logic),
        "dangling_aliases": len(dangling_aliases),
        "alias_cycles": len(alias_cycles),
    }
    static_complete = not any(static_gaps.values())

    return {
        "schema": "devagent-rockwell-capability-v9",
        "vendor": project.metadata.vendor,
        "engineering_tool": project.metadata.engineering_tool,
        "controller": project.metadata.controller_name,
        "processor_type": project.metadata.processor_type,
        "software_revision": project.metadata.software_revision,
        "source_sha256": project.metadata.source_sha256,
        "full_project": project.metadata.full_project,
        "routine_types": dict(sorted(routine_counts.items())),
        "routine_support": {
            "RLL": "BOUNDED_DETERMINISTIC",
            "ST": "BOUNDED_DETERMINISTIC",
            "FBD": "INVENTORY_ONLY_NOT_PROVEN",
            "SFC": "INVENTORY_ONLY_NOT_PROVEN",
            "OTHER": "INVENTORY_ONLY_NOT_PROVEN",
        },
        "instruction_semantics": {
            "total": project.instruction_total,
            "full_count": project.instruction_semantic_count,
            "coverage": project.instruction_semantic_coverage,
            "partial_names": sorted(project.partially_modeled_instruction_names, key=str.casefold),
            "partial_families": _instruction_family_profile(project),
            "unknown_names": sorted(project.unknown_instruction_names, key=str.casefold),
        },
        "branch_semantics": {
            "total": project.branch_rung_total,
            "modeled": project.branch_rung_semantic_count,
            "coverage": project.branch_semantic_coverage,
        },
        "structured_text": {
            "statements": project.st_statement_total,
            "modeled": project.st_statement_semantic_count,
            "coverage": project.st_semantic_coverage,
        },
        "aoi": {
            "definitions": project.aoi_internal_total,
            "modeled_bodies": project.aoi_internal_modeled_count,
            "calls": project.aoi_call_total,
            "bound_calls": project.aoi_call_bound_count,
            "protected_definitions": len(protected_aois),
        },
        "source_protection": {
            "protected_routines": len(protected_routines),
            "protected_aois": len(protected_aois),
        },
        "aliases": {
            "resolved": len(resolved_aliases),
            "dangling": len(dangling_aliases),
            "cycles": len(alias_cycles),
        },
        "indirect_addressing": {
            "rungs": len(indirect_rungs),
            "evidence_ids": indirect_rungs,
        },
        "static_gaps": static_gaps,
        "static_contract": "COMPLETE" if static_complete else "PARTIAL_FAIL_CLOSED",
        "dynamic_contract": {
            "qualified_backend_required_for_runtime_pass": True,
            "physical_controller_writes_default": False,
            "supported_execution_path": "FactoryTalk Logix Echo adapter or separately qualified backend evidence",
        },
    }


def rockwell_support_check(project) -> StaticCheck:
    profile = rockwell_capability_profile(project)
    gaps = profile["static_gaps"]
    evidence: list[str] = []

    evidence.extend(
        routine.id
        for routine in project.routines
        if routine.routine_type.upper() not in _SUPPORTED_ROUTINE_TYPES or routine.source_protected
    )
    evidence.extend(aoi.id for aoi in project.aois if aoi.source_protected or not aoi.internal_body_modeled)

    partial_or_unknown = {
        name.casefold()
        for name in [
            *project.partially_modeled_instruction_names,
            *project.unknown_instruction_names,
        ]
    }
    if partial_or_unknown:
        evidence.extend(
            rung.id
            for rung in project.rungs
            if any(instruction.name.casefold() in partial_or_unknown for instruction in rung.instructions)
        )

    evidence.extend(profile["indirect_addressing"]["evidence_ids"])
    _, dangling_aliases, alias_cycles = _alias_health(project)
    evidence.extend(dangling_aliases)
    evidence.extend(alias_cycles)
    if gaps["no_supported_logic"]:
        evidence.extend(routine.id for routine in project.routines)
    evidence = list(dict.fromkeys(evidence))

    nonzero = [f"{name}={value}" for name, value in gaps.items() if value]
    return StaticCheck(
        id="ROCKWELL_PRODUCTION_SUPPORT_CONTRACT",
        status=StaticCheckStatus.PASS if not nonzero else StaticCheckStatus.WARN,
        summary=(
            "Rockwell V9 production support contract is statically complete for the exported project. "
            "Runtime behavior still requires qualified execution evidence."
            if not nonzero
            else "Rockwell V9 support is fail-closed; static proof is withheld for: " + ", ".join(nonzero) + "."
        ),
        evidence=tuple(evidence),
    )


__all__ = [
    "augment_closeout_semantics",
    "instruction_family",
    "rockwell_capability_profile",
    "rockwell_support_check",
]
