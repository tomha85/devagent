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
_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "


def _has_neutral_text_branch(text: str) -> bool:
    """Detect Rockwell neutral-text branch syntax conservatively.

    V1 does not derive per-output dependencies across branch legs because collapsing a
    branched rung into one read/write set can create false Cartesian dependencies.
    """

    return "[" in text and "]" in text


def _populate_structured_unknown_instruction_names(project: CanonicalPLCProject) -> None:
    """Keep canonical machine output aligned with the parser's coverage warning."""

    if project.unknown_instruction_names:
        return
    names: set[str] = set()
    for warning in project.warnings:
        if not warning.startswith(_UNKNOWN_WARNING_PREFIX):
            continue
        payload = warning[len(_UNKNOWN_WARNING_PREFIX) :]
        names.update(name.strip() for name in payload.split(",") if name.strip())
    project.unknown_instruction_names = sorted(names)


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

        # Rockwell neutral text represents parallel branch legs with square brackets and
        # commas. Until V1 carries the branch AST explicitly, a rung-wide Cartesian
        # product would create false output dependencies for outputs inside separate legs.
        if _has_neutral_text_branch(rung.text):
            continue
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
    Branched neutral text is excluded until branch-path semantics are represented, so a
    test candidate cannot silently mix conditions from independent branch legs.
    """

    tests: list[FATTestCase] = []
    for rung in project.rungs:
        if _has_neutral_text_branch(rung.text):
            continue
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

    if project.rungs and all(
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
                summary=(
                    "No parsed RLL logic is available for source-provenance verification."
                    if not project.rungs
                    else "One or more parsed logic objects are missing source provenance."
                ),
            )
        )

    unsupported_types = sorted({routine.routine_type for routine in project.routines if routine.routine_type != "RLL"})
    protected_routine_count = sum(1 for routine in project.routines if routine.source_protected)
    protected_aoi_count = sum(1 for aoi in project.aois if aoi.source_protected)
    branch_rung_count = sum(1 for rung in project.rungs if _has_neutral_text_branch(rung.text))
    coverage_evidence = (
        f"semantic instructions={project.instruction_semantic_count}/{project.instruction_total}",
        f"non-RLL types={','.join(unsupported_types) if unsupported_types else 'none'}",
        f"protected routines={protected_routine_count}",
        f"protected AOIs={protected_aoi_count}",
        f"branched RLL rungs={branch_rung_count}",
    )
    if not project.rungs:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.NOT_PROVEN,
                summary="No RLL rungs were parsed; PLC logic semantic coverage cannot be proven.",
                evidence=coverage_evidence,
            )
        )
    elif (
        unsupported_types
        or protected_routine_count
        or protected_aoi_count
        or branch_rung_count
        or project.instruction_semantic_coverage < 1.0
    ):
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.WARN,
                summary=(
                    f"Deterministic instruction semantic coverage is "
                    f"{project.instruction_semantic_coverage:.1%}; unmodeled, protected, or branch-path logic is not treated as fully proven."
                ),
                evidence=coverage_evidence,
            )
        )
    else:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.PASS,
                summary="All parsed RLL instructions and dependency paths are covered by the V1 deterministic semantic model.",
                evidence=coverage_evidence,
            )
        )

    checks.append(
        StaticCheck(
            id="BRANCH_DEPENDENCY_SEMANTICS",
            status=StaticCheckStatus.WARN if branch_rung_count else StaticCheckStatus.PASS,
            summary=(
                f"{branch_rung_count} branched RLL rung(s) retain rung-level reads/writes, but per-output DEPENDS_ON edges and FAT candidates are withheld to avoid false cross-branch dependencies."
                if branch_rung_count
                else "No Rockwell neutral-text branch syntax was detected in parsed RLL rungs."
            ),
        )
    )

    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    checks.append(
        StaticCheck(
            id="DEPENDENCY_GRAPH",
            status=StaticCheckStatus.PASS if project.rungs and dependency_edges else StaticCheckStatus.WARN,
            summary=f"Dependency graph contains {len(graph.edges)} edges, including {len(dependency_edges)} proven tag dependencies.",
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
                else "No traceable simple, unbranched RLL FAT test candidates were generated."
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
    _populate_structured_unknown_instruction_names(project)
    graph = build_dependency_graph(project)
    fat_tests = generate_fat_tests(project)
    checks = static_verify(project, graph, fat_tests)

    unsupported_types = {routine.routine_type for routine in project.routines if routine.routine_type != "RLL"}
    protected_routine_count = sum(1 for routine in project.routines if routine.source_protected)
    protected_aoi_count = sum(1 for aoi in project.aois if aoi.source_protected)
    branch_rung_count = sum(1 for rung in project.rungs if _has_neutral_text_branch(rung.text))
    incomplete_semantics = project.instruction_semantic_coverage < 1.0
    no_parsed_logic = not project.rungs
    outcome = (
        PLCOutcome.PARTIALLY_VERIFIED
        if (
            unsupported_types
            or protected_routine_count
            or protected_aoi_count
            or branch_rung_count
            or incomplete_semantics
            or no_parsed_logic
        )
        else PLCOutcome.STATICALLY_VERIFIED
    )
    limitations = [
        "PLC V1 performs static analysis only; it does not execute Studio 5000, Logix Echo, or a real controller.",
        "FAT cases are engineering test candidates, not PASS results, until an execution backend observes expected behavior.",
        "The analyzer does not infer safety integrity level, required timing, or machine requirements that are absent from the project.",
        *project.warnings,
    ]
    if no_parsed_logic:
        limitations.append("No RLL rungs were parsed; controller logic verification remains NOT_PROVEN.")
    if protected_aoi_count:
        limitations.append(
            f"{protected_aoi_count} Add-On Instruction definition(s) contain encoded/protected content; their internal behavior remains NOT_PROVEN."
        )
    if branch_rung_count:
        limitations.append(
            f"{branch_rung_count} branched RLL rung(s) are retained as source facts, but V1 withholds per-output dependencies and FAT candidates until branch-path semantics are modeled."
        )
    return PLCEngineeringResult(
        outcome=outcome,
        project=project,
        graph=graph,
        fat_tests=fat_tests,
        static_checks=checks,
        limitations=limitations,
    )
