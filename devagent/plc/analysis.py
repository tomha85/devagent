from __future__ import annotations

import hashlib

from devagent.plc.models import (
    CanonicalPLCProject,
    FATTestCase,
    PLCDependencyEdge,
    PLCDependencyGraph,
    PLCEngineeringResult,
    PLCOutcome,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.rockwell_l5x import parse_full_project_l5x


_SIMPLE_FAT_INSTRUCTIONS = {"XIC", "XIO", "OTE", "OTL", "OTU"}
_OUTPUT_INSTRUCTIONS = {"OTE", "OTL", "OTU"}


def build_dependency_graph(project: CanonicalPLCProject) -> PLCDependencyGraph:
    edges: list[PLCDependencyEdge] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(source: str, target: str, kind: str, evidence_id: str) -> None:
        key = (source, target, kind, evidence_id)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            PLCDependencyEdge(
                source=source,
                target=target,
                kind=kind,
                evidence_id=evidence_id,
            )
        )

    for rung in project.rungs:
        for tag in rung.reads:
            add(rung.id, tag, "READS", rung.id)
        for tag in rung.writes:
            add(rung.id, tag, "WRITES", rung.id)
        for call in rung.calls:
            add(rung.id, call, "CALLS", rung.id)
        for reference in rung.references:
            if reference not in rung.reads and reference not in rung.writes:
                add(rung.id, reference, "REFERENCES", rung.id)
        for output in rung.writes:
            for dependency in rung.reads:
                add(output, dependency, "DEPENDS_ON", rung.id)

    return PLCDependencyGraph(
        edges=edges,
        unknown_instruction_names=list(project.unknown_instruction_names),
    )


def _first_argument(instruction) -> str | None:
    if not instruction.arguments:
        return None
    value = instruction.arguments[0].strip()
    return value or None


def generate_fat_tests(project: CanonicalPLCProject) -> list[FATTestCase]:
    """Generate conservative test candidates only for simple boolean RLL rungs.

    V1 intentionally avoids claiming that every contact is an interlock or permissive.
    The generated test establishes a traceable scenario for the rung; simulator-backed
    execution and requirement semantics are separate future verification layers.
    """

    tests: list[FATTestCase] = []
    for rung in project.rungs:
        names = {instruction.name.upper() for instruction in rung.instructions}
        if not names or not names.issubset(_SIMPLE_FAT_INSTRUCTIONS):
            continue

        preconditions: dict[str, bool] = {}
        contradictory = False
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in {"XIC", "XIO"}:
                continue
            tag = _first_argument(instruction)
            if tag is None:
                continue
            required = name == "XIC"
            if tag in preconditions and preconditions[tag] != required:
                contradictory = True
                break
            preconditions[tag] = required
        if contradictory or not preconditions:
            continue

        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in _OUTPUT_INSTRUCTIONS:
                continue
            output = _first_argument(instruction)
            if output is None:
                continue
            if name == "OTE":
                expected = f"{output}=TRUE while the evaluated rung-in condition is TRUE"
            elif name == "OTL":
                expected = f"{output}=TRUE (latched) after the evaluated rung-in condition becomes TRUE"
            else:
                expected = f"{output}=FALSE (unlatched) after the evaluated rung-in condition becomes TRUE"
            digest = hashlib.sha1(f"{rung.id}:{name}:{output}".encode("utf-8")).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-RLL-{digest}",
                    title=f"Exercise {output} logic at {rung.program}/{rung.routine} rung {rung.number}",
                    source=rung.source,
                    output_tag=output,
                    preconditions=dict(sorted(preconditions.items())),
                    expected=expected,
                    limitations=(
                        "Generated from static RLL structure; no PLC scan was executed.",
                        "Other writers, task ordering, I/O behavior, and retentive state are not simulated in V1.",
                    ),
                )
            )
    return tests


def static_verify(
    project: CanonicalPLCProject,
    graph: PLCDependencyGraph,
    fat_tests: list[FATTestCase],
) -> list[StaticCheck]:
    checks: list[StaticCheck] = [
        StaticCheck(
            id="L5X_FULL_PROJECT",
            status=StaticCheckStatus.PASS,
            summary="Artifact is a Rockwell L5X full-project Controller export.",
            evidence=(project.metadata.source_path, project.metadata.source_sha256),
        )
    ]

    if all(
        rung.source.artifact
        and rung.source.controller
        and rung.source.program
        and rung.source.routine
        and rung.source.rung is not None
        for rung in project.rungs
    ):
        checks.append(
            StaticCheck(
                id="SOURCE_PROVENANCE",
                status=StaticCheckStatus.PASS,
                summary=f"All {len(project.rungs)} parsed rungs retain source provenance.",
            )
        )
    else:
        checks.append(
            StaticCheck(
                id="SOURCE_PROVENANCE",
                status=StaticCheckStatus.NOT_PROVEN,
                summary="One or more parsed logic objects are missing source provenance.",
            )
        )

    unsupported_types = sorted({routine.routine_type for routine in project.routines if routine.routine_type != "RLL"})
    protected_count = sum(1 for routine in project.routines if routine.source_protected)
    coverage_evidence = (
        f"semantic instructions={project.instruction_semantic_count}/{project.instruction_total}",
        f"non-RLL types={','.join(unsupported_types) if unsupported_types else 'none'}",
        f"protected routines={protected_count}",
    )
    if unsupported_types or protected_count or project.instruction_semantic_coverage < 1.0:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.WARN,
                summary=(
                    f"Deterministic instruction semantic coverage is "
                    f"{project.instruction_semantic_coverage:.1%}; unmodeled or inaccessible logic is not treated as proven."
                ),
                evidence=coverage_evidence,
            )
        )
    else:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.PASS,
                summary="All parsed RLL instructions are covered by the V1 deterministic semantic model.",
                evidence=coverage_evidence,
            )
        )

    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    checks.append(
        StaticCheck(
            id="DEPENDENCY_GRAPH",
            status=StaticCheckStatus.PASS if project.rungs and dependency_edges else StaticCheckStatus.WARN,
            summary=f"Dependency graph contains {len(graph.edges)} edges, including {len(dependency_edges)} tag dependencies.",
        )
    )

    rung_ids = {rung.id for rung in project.rungs}
    traceable = all(
        any(rung.id in rung_ids and rung.source == test.source for rung in project.rungs)
        for test in fat_tests
    )
    checks.append(
        StaticCheck(
            id="FAT_TEST_TRACEABILITY",
            status=(
                StaticCheckStatus.PASS
                if fat_tests and traceable
                else StaticCheckStatus.WARN
            ),
            summary=(
                f"{len(fat_tests)} conservative FAT test candidate(s) are traceable to source rungs."
                if fat_tests and traceable
                else "No traceable simple-RLL FAT test candidates were generated."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="SIMULATOR_EXECUTION",
            status=StaticCheckStatus.NOT_PROVEN,
            summary="Simulator execution is not part of Rockwell PLC V1; no dynamic machine behavior is claimed as verified.",
        )
    )
    return checks


def analyze_rockwell_l5x(path) -> PLCEngineeringResult:
    project = parse_full_project_l5x(path)
    graph = build_dependency_graph(project)
    fat_tests = generate_fat_tests(project)
    checks = static_verify(project, graph, fat_tests)

    unsupported_types = {routine.routine_type for routine in project.routines if routine.routine_type != "RLL"}
    protected = any(routine.source_protected for routine in project.routines)
    incomplete_semantics = project.instruction_semantic_coverage < 1.0
    no_parsed_logic = bool(project.routines) and not project.rungs
    outcome = (
        PLCOutcome.PARTIALLY_VERIFIED
        if unsupported_types or protected or incomplete_semantics or no_parsed_logic
        else PLCOutcome.STATICALLY_VERIFIED
    )
    limitations = [
        "PLC V1 performs static analysis only; it does not execute Studio 5000, Logix Echo, or a real controller.",
        "FAT cases are engineering test candidates, not PASS results, until an execution backend observes expected behavior.",
        "The analyzer does not infer safety integrity level, required timing, or machine requirements that are absent from the project.",
        *project.warnings,
    ]
    return PLCEngineeringResult(
        outcome=outcome,
        project=project,
        graph=graph,
        fat_tests=fat_tests,
        static_checks=checks,
        limitations=limitations,
    )
