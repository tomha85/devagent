from __future__ import annotations

from dataclasses import replace

from devagent.plc.models import PLCEngineeringResult, PLCSemanticState, StaticCheckStatus
from devagent.plc import schneider_graphical_v4 as _v4
from devagent.plc import schneider_control_expert_v1 as _v1


_INSTALLED = False
_PREVIOUS_ANALYZER = _v4.analyze_schneider_control_expert_v4


def analyze_schneider_control_expert_v4_hardened(path):
    result = _PREVIOUS_ANALYZER(path)
    project = result.project
    facts = _v4._facts(project)
    if facts is None or not facts.writer_conflicts:
        return result

    conflicts = {item.casefold() for item in facts.writer_conflicts}
    retained = set(facts.modeled_logic_ids)
    changed = False
    regions = []
    for region in facts.regions:
        if not any(write.casefold() in conflicts for write in region.writes):
            regions.append(region)
            continue
        changed = True
        regions.append(
            replace(
                region,
                semantic_state=PLCSemanticState.PARTIAL,
                reason="competing_output_writer",
                logic_ids=tuple(item for item in region.logic_ids if item in retained),
            )
        )
    if not changed:
        return result

    updated_facts = replace(facts, regions=tuple(regions))
    setattr(project, "_schneider_v4_facts", updated_facts)

    fat_tests = list(result.fat_tests)
    fat_tests.extend(_v4._gap_fat(project, updated_facts))
    fat_tests = list({item.id: item for item in fat_tests}.values())

    checks = []
    for check in result.static_checks:
        if check.id != "SCHNEIDER_V4_GRAPHICAL_SEMANTICS":
            checks.append(check)
            continue
        checks.append(
            replace(
                check,
                status=StaticCheckStatus.NOT_PROVEN,
                summary=(
                    f"Deterministically retained {len(updated_facts.modeled)} FULL Schneider LD/FBD graphical region(s); "
                    f"{len(updated_facts.partial)} region(s) are PARTIAL and {len(updated_facts.withheld)} OPAQUE after writer-ownership reconciliation."
                ),
                evidence=tuple(item.id for item in updated_facts.regions),
            )
        )

    return PLCEngineeringResult(
        result.outcome,
        project,
        result.graph,
        fat_tests,
        checks,
        result.limitations,
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch

    _v4.analyze_schneider_control_expert_v4 = analyze_schneider_control_expert_v4_hardened
    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v4_hardened
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v4_hardened
    _INSTALLED = True


__all__ = ["analyze_schneider_control_expert_v4_hardened", "install"]
