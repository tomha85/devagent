from __future__ import annotations

from devagent.plc import analysis as _base
from devagent.plc.models import PLCOutcome, StaticCheckStatus
from devagent.plc.rockwell_structure import (
    add_rockwell_structure_edges,
    augment_rockwell_structure,
    rockwell_structure_check,
)
from devagent.plc.v2_guardrails import enforce_v2_guardrails, verify_v2_source_unchanged


_BASE_ANALYZE = _base.analyze_rockwell_l5x


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


def _limitations(project, state) -> list[str]:
    result = [
        "PLC V2 performs deterministic static analysis only; it does not execute Studio 5000, Logix Echo, or a real controller.",
        "FAT cases are engineering test candidates, not PASS results, until an execution backend observes expected behavior.",
        "The analyzer does not infer safety integrity level, required timing, or machine requirements that are absent from the project.",
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
            f"{state['unmodeled_st']} Structured Text statement(s) contain control/call semantics outside the bounded V2 ST model and remain PARTIAL."
        )
    if state["indirect"]:
        result.append(
            f"{state['indirect']} RLL rung(s) use variable array subscripts; index references are retained, but V2 withholds output-path FAT until an index value is fixed."
        )
    if project.partially_modeled_instruction_names:
        result.append(
            "Recognized Rockwell instructions remain directionally PARTIAL and do not contribute to fully proven instruction coverage: "
            + ", ".join(project.partially_modeled_instruction_names)
        )
    return result


def analyze_rockwell_l5x(path):
    """Run the guarded Rockwell analyzer with deterministic V2/V7 augmentation."""

    result = _BASE_ANALYZE(path)
    # The base parser records the source hash before later passes re-read the
    # L5X. Reject ordinary source changes before any augmentation can mix
    # provenance from different project bytes.
    verify_v2_source_unchanged(result.project)

    project = result.project
    augment_rockwell_structure(project)
    enforce_v2_guardrails(project)
    graph = _base.build_dependency_graph(project)
    _filter_unproven_rll_statement_dependencies(project, graph)
    add_rockwell_structure_edges(project, graph)
    fat_tests = _base.generate_fat_tests(project)
    checks = _base.static_verify(project, graph, fat_tests)
    structure_check = rockwell_structure_check(project)
    checks.append(structure_check)
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
    )

    result.outcome = PLCOutcome.PARTIALLY_VERIFIED if incomplete else PLCOutcome.STATICALLY_VERIFIED
    result.graph = graph
    result.fat_tests = fat_tests
    result.static_checks = checks
    result.limitations = _limitations(project, state)
    return result
