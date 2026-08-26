from __future__ import annotations

from collections import Counter

from devagent.plc.models import StaticCheck, StaticCheckStatus


_WARNING_PREFIX = "Rockwell V10 standard catalog: "

# Classification-only catalog. Presence in this table means DevAgent recognizes
# the mnemonic as a Rockwell/Logix instruction family; it DOES NOT mean behavior
# is deterministically modeled. Dedicated theorems remove instructions from the
# need for this catalog by proving their behavior elsewhere.
_FAMILIES: dict[str, frozenset[str]] = {
    "EDGE_STATE": frozenset({"ONS", "OSR", "OSF"}),
    "SYSTEM_ATTRIBUTE": frozenset({"GSV", "SSV"}),
    "FILE_ARRAY": frozenset(
        {
            "FAL", "FBC", "FSC", "FFL", "FFU", "LFL", "LFU", "FLL",
            "DDT", "BTD", "BSL", "BSR", "SWPB", "MVM", "SIZE", "AVE", "STD", "SRT",
        }
    ),
    "SEQUENCER": frozenset({"SQI", "SQO", "SQL"}),
    "COMMUNICATION": frozenset({"MSG"}),
    "PROCESS_CONTROL": frozenset({"PID", "PIDE"}),
    "PROGRAM_CONTROL": frozenset(
        {"MCR", "JMP", "LBL", "TND", "UID", "UIE", "SFR", "SFP", "EVENT", "EOT"}
    ),
    "ASCII": frozenset({"ABL", "ACB", "ACL", "AEX", "AHL", "ARD", "ARL", "AWA", "AWT"}),
    "ALARM": frozenset({"ALMA", "ALMD"}),
    "EXPRESSION_COMPARE": frozenset({"CMP"}),
    "SELECT_LIMIT_STATISTICAL": frozenset({"SEL", "MIN", "MAX"}),
    "CONVERSION": frozenset({"TOD", "FRD"}),
}

_INSTRUCTION_TO_FAMILY = {
    name: family
    for family, names in _FAMILIES.items()
    for name in names
}


def standard_family(name: str) -> str | None:
    return _INSTRUCTION_TO_FAMILY.get(name.upper())


def augment_standard_catalog(project) -> None:
    """Convert known-standard but unproven instructions from UNKNOWN to PARTIAL.

    This pass is deliberately classification-only. It never creates dependency
    proof, output logic, requirement verification, or executable FAT behavior.
    """
    present: dict[str, set[str]] = {}
    for rung in project.rungs:
        for instruction in rung.instructions:
            family = standard_family(instruction.name)
            if family is None:
                continue
            present.setdefault(family, set()).add(instruction.name.upper())

    if not present:
        return

    names = {name for values in present.values() for name in values}
    unknown_folded = {name.casefold() for name in names}
    project.unknown_instruction_names = [
        name for name in project.unknown_instruction_names
        if name.casefold() not in unknown_folded
    ]
    project.partially_modeled_instruction_names = sorted(
        set(project.partially_modeled_instruction_names) | names,
        key=str.casefold,
    )

    retained = [warning for warning in project.warnings if not warning.startswith(_WARNING_PREFIX)]
    for family in sorted(present):
        retained.append(
            _WARNING_PREFIX
            + f"{family} instruction(s) recognized but behavior remains PARTIAL: "
            + ", ".join(sorted(present[family]))
        )
    project.warnings = retained


def standard_catalog_profile(project) -> dict[str, object]:
    occurrences: Counter[str] = Counter()
    names: dict[str, set[str]] = {}
    for rung in project.rungs:
        for instruction in rung.instructions:
            family = standard_family(instruction.name)
            if family is None:
                continue
            occurrences[family] += 1
            names.setdefault(family, set()).add(instruction.name.upper())
    return {
        "schema": "devagent-rockwell-standard-catalog-v1",
        "occurrences": sum(occurrences.values()),
        "families": {
            family: {
                "occurrences": occurrences[family],
                "instructions": sorted(names.get(family, set())),
                "behavior": "PARTIAL_FAIL_CLOSED",
            }
            for family in sorted(occurrences)
        },
    }


def standard_catalog_check(project) -> StaticCheck:
    profile = standard_catalog_profile(project)
    if not profile["occurrences"]:
        return StaticCheck(
            id="ROCKWELL_STANDARD_INSTRUCTION_CATALOG",
            status=StaticCheckStatus.PASS,
            summary="No additional standard Rockwell instruction families require classification-only handling.",
        )
    family_names = ", ".join(profile["families"])
    return StaticCheck(
        id="ROCKWELL_STANDARD_INSTRUCTION_CATALOG",
        status=StaticCheckStatus.WARN,
        summary=(
            f"Recognized {profile['occurrences']} standard Rockwell instruction occurrence(s) in classification-only families ({family_names}); "
            "their behavior remains PARTIAL until a dedicated deterministic or runtime theorem is qualified."
        ),
    )


__all__ = [
    "augment_standard_catalog",
    "standard_catalog_check",
    "standard_catalog_profile",
    "standard_family",
]
