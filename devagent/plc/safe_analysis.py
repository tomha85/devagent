from __future__ import annotations

from devagent.plc import analysis as _base
from devagent.plc.models import PLCDependencyEdge, PLCOutcome, StaticCheck, StaticCheckStatus
from devagent.plc.rockwell_closeout import augment_closeout_semantics, rockwell_support_check
from devagent.plc.rockwell_compare import (
    augment_compare_instruction_semantics,
    compare_models,
    generate_compare_fat_tests,
    rockwell_compare_check,
)
from devagent.plc.rockwell_general_actions import (
    add_action_dependencies,
    generate_action_fat_tests,
    rockwell_action_check,
)
from devagent.plc.rockwell_structure import (
    add_rockwell_structure_edges,
    augment_rockwell_structure,
    rockwell_structure_check,
)
from devagent.plc.v2_guardrails import enforce_v2_guardrails, verify_v2_source_unchanged


_BASE_ANALYZE = _base.analyze_rockwell_l5x
_COMPLEX_COMPARE_NAMES = {"LIM", "LIMIT", "MEQ"}


def _filter_unproven_rll_statement_dependencies(project, graph) -> None:
    rll_statement_ids = {
        statement.id
        for statement in project.logic_statements
        if statement.language == "RLL"
    }
    if not rll_statement_ids:
        return
    graph.edges = [
        edge
        for edge in graph.edges
        if not (edge.kind == "DEPENDS_ON" and edge.evidence_id in rll_statement_ids)
    ]


def _add_compare_dependencies(project, graph) -> None:
    seen = {(edge.source, edge.target, edge.kind, edge.evidence_id) for edge in graph.edges}
    for model in compare_models(project):
        for dependency in (model.input_tag, *(tag for tag, _ in model.contacts)):
            key = (model.output_tag, dependency, "DEPENDS_ON", model.rung_id)
            if key in seen:
                continue
            seen.add(key)
            graph.edges.append(
                PLCDependencyEdge(
                    source=model.output_tag,
                    target=dependency,
                    kind="DEPENDS_ON",
                    evidence_id=model.rung_id,
                )
            )


def _bounded_compare_check(project):
    check = rockwell_compare_check(project)
    complex_rungs = [
        rung.id
        for rung in project.rungs
        if any(instruction.name.upper() in _COMPLEX_COMPARE_NAMES for instruction in rung.instructions)
    ]
    if not complex_rungs:
        return check
    evidence = tuple(dict.fromkeys([*check.evidence, *complex_rungs]))
    return StaticCheck(
        id=check.id,
        status=StaticCheckStatus.WARN,
        summary=(
            check.summary
            + f" {len(complex_rungs)} LIM/LIMIT/MEQ rung(s) remain directionally recognized but withheld from typed output-threshold proof."
        ),
        evidence=evidence,
    )


def _limitations(project, state, compare_check, support_check) -> list[str]:
    result = [
        "PLC static analysis does not by itself execute Studio 5000, Logix Echo, or a real controller.",
        "FAT cases are engineering test candidates, not PASS results, until an execution backend observes expected behavior.",
        "The analyzer does not infer safety integrity level, required timing, process physics, or machine requirements that are absent from the project.",
        *project.warnings,
    ]
    if state["no_logic"]:
        result.append("No executable supported logic was normalized; controller behavior remains NOT_PROVEN.")
    if state["unmodeled_aois"]:
        result.append(
            f"{state['unmodeled_aois']} Add-On Instruction definition(s) contain unsupported, protected, or partial internal logic; their behavior remains NOT_PROVEN."
        )
    if state["unresolved_aoi_calls"]:
        result.append(
            f"{state['unresolved_aoi_calls']} AOI invocation(s) could not be directionally bound to an exported backing tag/interface and remain reference-only."
        )
    if state["unmodeled_branches"]:
        result.append(
            f"{state['unmodeled_branches']} branched RLL rung(s) contain logic outside the bounded XIC/XIO/OTE/OTL/OTU boolean-path model; derived output dependencies/FAT are withheld for those rungs."
        )
    if state["unmodeled_st"]:
        result.append(
            f"{state['unmodeled_st']} Structured Text statement(s) contain control/call semantics outside the bounded ST model and remain PARTIAL."
        )
    if state["indirect"]:
        result.append(
            f"{state['indirect']} RLL rung(s) use variable array subscripts; output-path FAT is withheld until an index value is fixed."
        )
    if compare_check.status is not StaticCheckStatus.PASS:
        result.append(
            "One or more compare-bearing RLL rungs are outside the bounded single-compare linear OTE model; typed threshold proof/FAT is withheld for those rungs."
        )
    if support_check.status is not StaticCheckStatus.PASS:
        result.append(
            "Rockwell V9 production support contract is PARTIAL for this export. Unsupported routine types, protected logic, unresolved aliases, or complex instruction families remain outside static VERIFIED claims."
        )
    if project.partially_modeled_instruction_names:
        result.append(
            "Recognized Rockwell instructions remain directionally PARTIAL and do not contribute to fully proven instruction coverage: "
            + ", ".join(project.partially_modeled_instruction_names)
        )
    return list(dict.fromkeys(result))


def analyze_rockwell_l5x(path):
    """Run guarded Rockwell production analysis with fail-closed semantics."""

    result = _BASE_ANALYZE(path)
    # The base parser records the source hash before later passes re-read the
    # L5X. Reject ordinary source changes before any augmentation can mix
    # provenance from different project bytes.
    verify_v2_source_unchanged(result.project)

    project = result.project
    augment_rockwell_structure(project)
    # Studio 5000 v36+ renamed several compare mnemonics. Normalize those
    # aliases after the base pass without inventing semantics for complex rungs.
    augment_compare_instruction_semantics(project)
    enforce_v2_guardrails(project)
    # Classify additional Rockwell instruction families as PARTIAL rather than
    # UNKNOWN. Classification alone never creates behavioral proof/FAT.
    augment_closeout_semantics(project)

    graph = _base.build_dependency_graph(project)
    _filter_unproven_rll_statement_dependencies(project, graph)
    add_rockwell_structure_edges(project, graph)
    _add_compare_dependencies(project, graph)
    # Add dependency edges only for action instructions whose complete rung-in
    # grammar and fixed destination identity are deterministically modeled.
    add_action_dependencies(project, graph)

    fat_tests = _base.generate_fat_tests(project)
    known_ids = {item.id for item in fat_tests}
    for item in generate_compare_fat_tests(project):
        if item.id not in known_ids:
            fat_tests.append(item)
            known_ids.add(item.id)
    for item in generate_action_fat_tests(project):
        if item.id not in known_ids:
            fat_tests.append(item)
            known_ids.add(item.id)

    checks = _base.static_verify(project, graph, fat_tests)
    structure_check = rockwell_structure_check(project)
    compare_check = _bounded_compare_check(project)
    action_check = rockwell_action_check(project)
    support_check = rockwell_support_check(project)
    checks.extend((structure_check, compare_check, action_check, support_check))
    state = _base._coverage_state(project)
    incomplete = (
        any(
            state[key]
            for key in (
                "unsupported_types", "protected_routines", "protected_aois", "unmodeled_aois",
                "unresolved_aoi_calls", "unmodeled_branches", "unmodeled_st", "indirect", "no_logic",
                "incomplete_instruction", "partial_instructions",
            )
        )
        or structure_check.status is not StaticCheckStatus.PASS
        or compare_check.status is not StaticCheckStatus.PASS
        or action_check.status is not StaticCheckStatus.PASS
        or support_check.status is not StaticCheckStatus.PASS
    )

    result.outcome = PLCOutcome.PARTIALLY_VERIFIED if incomplete else PLCOutcome.STATICALLY_VERIFIED
    result.graph = graph
    result.fat_tests = fat_tests
    result.static_checks = checks
    limitations = _limitations(project, state, compare_check, support_check)
    if action_check.status is not StaticCheckStatus.PASS:
        limitations.append(
            "One or more reachable MOV/MOVE/COP/CPS/CLR/math/CPT/RES rungs contain control semantics or destination addressing outside the bounded action-path theorem; behavioral FAT is withheld for those rungs."
        )
    result.limitations = list(dict.fromkeys(limitations))
    return result
