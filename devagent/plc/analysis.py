from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import replace

from devagent.plc.models import (
    CanonicalPLCProject,
    FATTestCase,
    PLCDependencyEdge,
    PLCDependencyGraph,
    PLCEngineeringResult,
    PLCOutcome,
    PLCRung,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.rockwell_l5x import parse_full_project_l5x
from devagent.plc.v2_semantics import apply_v2_semantics


_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "
_NORMALIZED_VENDOR_INSTRUCTIONS = {"GSV", "SSV", "MOVE"}
_TIMER_COUNTER_INSTRUCTIONS = {"TON", "TOF", "RTO", "CTU", "CTD"}
_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
)
_SUBSCRIPT = re.compile(r"\[([^\]]+)\]")


def _has_neutral_text_branch(text: str) -> bool:
    paren_depth = 0
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "(":
            paren_depth += 1
            continue
        if char == ")":
            if paren_depth:
                paren_depth -= 1
            continue
        if char == "[" and paren_depth == 0:
            return True
    return False


def _operand_refs(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    if not stripped or stripped[0:1] in {'"', "'"}:
        return ()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", stripped):
        return ()
    if re.fullmatch(r"\d+#(?:[0-9A-Fa-f_]+)", stripped):
        return ()
    result: list[str] = []
    for token in _IDENTIFIER.findall(stripped):
        if token.lower() in {"true", "false", "and", "or", "not"}:
            continue
        if token not in result:
            result.append(token)
    return tuple(result)


def _first_operand_ref(arguments: tuple[str, ...], index: int = 0) -> str | None:
    if len(arguments) <= index:
        return None
    refs = _operand_refs(arguments[index])
    return refs[0] if refs else None


def _variable_subscript_refs(value: str) -> tuple[str, ...]:
    refs: list[str] = []
    for match in _SUBSCRIPT.finditer(value):
        expression = match.group(1).strip()
        if re.fullmatch(r"[-+]?\d+", expression):
            continue
        for ref in _operand_refs(expression):
            if ref not in refs:
                refs.append(ref)
    return tuple(refs)


def _rung_has_variable_array_subscript(rung: PLCRung) -> bool:
    return any(
        _variable_subscript_refs(argument)
        for instruction in rung.instructions
        for argument in instruction.arguments
    )


def _expand_expression_refs(values: set[str]) -> set[str]:
    expanded: set[str] = set()
    for value in values:
        if "-" in value:
            refs = _operand_refs(value)
            if refs:
                expanded.update(refs)
                continue
        expanded.add(value)
    return expanded


def _populate_structured_unknown_instruction_names(project: CanonicalPLCProject) -> None:
    if project.unknown_instruction_names:
        return
    names: set[str] = set()
    for warning in project.warnings:
        if not warning.startswith(_UNKNOWN_WARNING_PREFIX):
            continue
        payload = warning[len(_UNKNOWN_WARNING_PREFIX) :]
        names.update(name.strip() for name in payload.split(",") if name.strip())
    project.unknown_instruction_names = sorted(names)


def _normalize_vendor_instruction_aliases(project: CanonicalPLCProject) -> None:
    """Preserve the V1 operand hardening before V2 structural reasoning."""
    unknown_upper = {name.upper() for name in project.unknown_instruction_names}
    newly_supported_occurrences = 0
    normalized_rungs: list[PLCRung] = []
    aoi_names = {aoi.name for aoi in project.aois}

    for rung in project.rungs:
        reads = _expand_expression_refs(set(rung.reads))
        writes = _expand_expression_refs(set(rung.writes))
        calls = set(rung.calls)
        references = _expand_expression_refs(set(rung.references))
        aoi_operand_refs: set[str] = set()

        for instruction in rung.instructions:
            name = instruction.name.upper()
            args = instruction.arguments
            for argument in args:
                for index_ref in _variable_subscript_refs(argument):
                    reads.add(index_ref)
                    references.add(index_ref)

            if name in {"OSR", "OSF"}:
                storage = _first_operand_ref(args, 0)
                output = _first_operand_ref(args, 1)
                if storage:
                    reads.add(storage)
                    writes.add(storage)
                if output:
                    writes.add(output)
            elif name in _TIMER_COUNTER_INSTRUCTIONS:
                structure = _first_operand_ref(args, 0)
                if structure:
                    reads.add(structure)
                    writes.add(structure)
                if len(args) > 1:
                    reads.update(_operand_refs(args[1]))
                accumulator = _first_operand_ref(args, 2)
                if accumulator:
                    writes.add(accumulator)
            elif name == "MOVE":
                if args:
                    reads.update(_operand_refs(args[0]))
                destination = _first_operand_ref(args, 1)
                if destination:
                    writes.add(destination)
                if name in unknown_upper:
                    newly_supported_occurrences += 1
            elif name == "GSV":
                destination = _first_operand_ref(args, 3)
                if destination:
                    writes.add(destination)
                if name in unknown_upper:
                    newly_supported_occurrences += 1
            elif name == "SSV":
                if len(args) > 3:
                    reads.update(_operand_refs(args[3]))
                if name in unknown_upper:
                    newly_supported_occurrences += 1
            elif name == "CPT":
                destination = _first_operand_ref(args, 0)
                if destination:
                    writes.add(destination)
                for value in args[1:]:
                    reads.update(_operand_refs(value))
            elif instruction.name in aoi_names:
                calls.add(instruction.name)
                for value in args:
                    aoi_operand_refs.update(_operand_refs(value))

        if aoi_operand_refs:
            reads.difference_update(aoi_operand_refs)
            writes.difference_update(aoi_operand_refs)
            references.update(aoi_operand_refs)

        references.update(reads)
        references.update(writes)
        normalized_rungs.append(
            replace(
                rung,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                calls=tuple(sorted(calls)),
                references=tuple(sorted(references)),
            )
        )

    project.rungs = normalized_rungs
    if newly_supported_occurrences:
        project.instruction_semantic_count = min(
            project.instruction_total,
            project.instruction_semantic_count + newly_supported_occurrences,
        )

    remaining_unknown = sorted(
        name
        for name in project.unknown_instruction_names
        if name.upper() not in _NORMALIZED_VENDOR_INSTRUCTIONS
    )
    project.unknown_instruction_names = remaining_unknown
    retained = [warning for warning in project.warnings if not warning.startswith(_UNKNOWN_WARNING_PREFIX)]
    if remaining_unknown:
        retained.append(f"{_UNKNOWN_WARNING_PREFIX}{', '.join(remaining_unknown)}")
    project.warnings = retained


def _scoped_ref(owner_type: str, owner_name: str, value: str) -> str:
    if owner_type == "aoi":
        return f"AOI:{owner_name}::{value}"
    return value


def build_dependency_graph(project: CanonicalPLCProject) -> PLCDependencyGraph:
    edges: list[PLCDependencyEdge] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(source: str, target: str, kind: str, evidence_id: str) -> None:
        key = (source, target, kind, evidence_id)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            PLCDependencyEdge(source=source, target=target, kind=kind, evidence_id=evidence_id)
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

    for statement in project.logic_statements:
        for tag in statement.reads:
            add(statement.id, _scoped_ref(statement.owner_type, statement.owner_name, tag), "READS", statement.id)
        for tag in statement.writes:
            add(statement.id, _scoped_ref(statement.owner_type, statement.owner_name, tag), "WRITES", statement.id)
        for call in statement.calls:
            add(statement.id, call, "CALLS", statement.id)
        if statement.semantic_state is PLCSemanticState.FULL:
            for output in statement.writes:
                for dependency in statement.reads:
                    add(
                        _scoped_ref(statement.owner_type, statement.owner_name, output),
                        _scoped_ref(statement.owner_type, statement.owner_name, dependency),
                        "DEPENDS_ON",
                        statement.id,
                    )

    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        owner_type = "aoi" if logic.source.aoi and logic.origin.startswith("AOI_INTERNAL:") else "program"
        owner_name = logic.source.aoi or logic.source.program or "controller"
        output = _scoped_ref(owner_type, owner_name, logic.output_tag)
        for path in logic.paths:
            for term in path.terms:
                add(output, _scoped_ref(owner_type, owner_name, term.tag), "DEPENDS_ON", logic.id)

    return PLCDependencyGraph(
        edges=edges,
        unknown_instruction_names=list(project.unknown_instruction_names),
    )


def _expected_true(instruction: str, output: str) -> str:
    if instruction == "OTE":
        return f"{output}=TRUE while the evaluated logic path is TRUE"
    if instruction == "OTL":
        return f"{output}=TRUE (latched) after the evaluated logic path becomes TRUE"
    return f"{output}=FALSE (unlatched) after the evaluated logic path becomes TRUE"


def _path_preconditions(path) -> dict[str, bool]:
    return {term.tag: term.required for term in path.terms}


def _negative_assignment(paths) -> dict[str, bool] | None:
    variables = sorted({term.tag for path in paths for term in path.terms})
    if not variables or len(variables) > 8:
        return None
    for values in itertools.product((False, True), repeat=len(variables)):
        assignment = dict(zip(variables, values))
        result = any(
            all(assignment.get(term.tag) == term.required for term in path.terms)
            for path in paths
        )
        if not result:
            return assignment
    return None


def generate_fat_tests(project: CanonicalPLCProject) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        if logic.origin.startswith("AOI_INTERNAL:"):
            continue
        for index, path in enumerate(logic.paths, start=1):
            preconditions = _path_preconditions(path)
            if not preconditions:
                continue
            digest = hashlib.sha1(f"{logic.id}:positive:{index}".encode("utf-8")).hexdigest()[:10]
            suffix = f" path {index}" if len(logic.paths) > 1 else ""
            tests.append(
                FATTestCase(
                    id=f"FAT-RLL-{digest}",
                    title=f"Exercise {logic.output_tag}{suffix} at {logic.source.locator}",
                    source=logic.source,
                    output_tag=logic.output_tag,
                    preconditions=dict(sorted(preconditions.items())),
                    expected=_expected_true(logic.instruction, logic.output_tag),
                    limitations=(
                        "Generated from deterministic static boolean-path semantics; no PLC scan was executed.",
                        "Other writers, task ordering, I/O behavior, timing, and retentive state are not simulated in V2.",
                    ),
                    scenario="POSITIVE_PATH",
                )
            )

        if logic.instruction != "OTE":
            continue
        if len(logic.paths) == 1:
            base = _path_preconditions(logic.paths[0])
            for tag in sorted(base):
                blocked = dict(base)
                blocked[tag] = not blocked[tag]
                digest = hashlib.sha1(f"{logic.id}:negative:{tag}".encode("utf-8")).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-RLL-{digest}",
                        title=f"Block {logic.output_tag} by toggling {tag} at {logic.source.locator}",
                        source=logic.source,
                        output_tag=logic.output_tag,
                        preconditions=dict(sorted(blocked.items())),
                        expected=f"{logic.output_tag}=FALSE while the complete rung-in condition is FALSE",
                        limitations=(
                            "Generated from deterministic static boolean-path semantics; no PLC scan was executed.",
                            "Other writers, task ordering, I/O behavior, timing, and retentive state are not simulated in V2.",
                        ),
                        scenario="NEGATIVE_BLOCK",
                    )
                )
        else:
            blocked = _negative_assignment(logic.paths)
            if blocked:
                digest = hashlib.sha1(f"{logic.id}:negative:branch".encode("utf-8")).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-RLL-{digest}",
                        title=f"Block all paths to {logic.output_tag} at {logic.source.locator}",
                        source=logic.source,
                        output_tag=logic.output_tag,
                        preconditions=dict(sorted(blocked.items())),
                        expected=f"{logic.output_tag}=FALSE while every modeled parallel path is FALSE",
                        limitations=(
                            "Generated from deterministic static branch semantics; no PLC scan was executed.",
                            "Other writers, task ordering, I/O behavior, timing, and retentive state are not simulated in V2.",
                        ),
                        scenario="NEGATIVE_BRANCH",
                    )
                )
    return tests


def _coverage_state(project: CanonicalPLCProject):
    unsupported_types = sorted(
        {
            routine.routine_type
            for routine in project.routines
            if routine.routine_type not in {"RLL", "ST"}
        }
    )
    protected_routines = sum(1 for routine in project.routines if routine.source_protected)
    protected_aois = sum(1 for aoi in project.aois if aoi.source_protected)
    unmodeled_aois = project.aoi_internal_total - project.aoi_internal_modeled_count
    unresolved_aoi_calls = project.aoi_call_total - project.aoi_call_bound_count
    unmodeled_branches = project.branch_rung_total - project.branch_rung_semantic_count
    unmodeled_st = project.st_statement_total - project.st_statement_semantic_count
    indirect = sum(1 for rung in project.rungs if _rung_has_variable_array_subscript(rung))
    instruction_count = sum(len(rung.instructions) for rung in project.rungs)
    no_logic = (
        instruction_count == 0
        and project.st_statement_total == 0
        and not any(aoi.internal_body_modeled for aoi in project.aois)
    )
    return {
        "unsupported_types": unsupported_types,
        "protected_routines": protected_routines,
        "protected_aois": protected_aois,
        "unmodeled_aois": max(0, unmodeled_aois),
        "unresolved_aoi_calls": max(0, unresolved_aoi_calls),
        "unmodeled_branches": max(0, unmodeled_branches),
        "unmodeled_st": max(0, unmodeled_st),
        "indirect": indirect,
        "no_logic": no_logic,
        "incomplete_instruction": project.instruction_semantic_coverage < 1.0,
        "partial_instructions": bool(project.partially_modeled_instruction_names),
    }


def static_verify(project: CanonicalPLCProject, graph: PLCDependencyGraph, fat_tests: list[FATTestCase]) -> list[StaticCheck]:
    state = _coverage_state(project)
    checks: list[StaticCheck] = [
        StaticCheck(
            id="L5X_FULL_PROJECT",
            status=StaticCheckStatus.PASS,
            summary="Artifact is a Rockwell L5X full-project Controller export.",
            evidence=(project.metadata.source_path, project.metadata.source_sha256),
        )
    ]

    source_objects = [*project.rungs, *project.logic_statements]
    provenance_ok = bool(source_objects) and all(
        item.source.artifact and item.source.controller and item.source.routine
        for item in source_objects
    )
    checks.append(
        StaticCheck(
            id="SOURCE_PROVENANCE",
            status=StaticCheckStatus.PASS if provenance_ok else StaticCheckStatus.NOT_PROVEN,
            summary=(
                f"All {len(source_objects)} normalized logic object(s) retain source provenance."
                if provenance_ok
                else "No normalized executable logic, or one or more logic objects lack source provenance."
            ),
        )
    )

    coverage_evidence = (
        f"directional RLL instructions={project.instruction_semantic_count}/{project.instruction_total}",
        f"ST statements modeled={project.st_statement_semantic_count}/{project.st_statement_total}",
        f"branch rungs modeled={project.branch_rung_semantic_count}/{project.branch_rung_total}",
        f"AOI internal bodies modeled={project.aoi_internal_modeled_count}/{project.aoi_internal_total}",
        f"AOI call interfaces bound={project.aoi_call_bound_count}/{project.aoi_call_total}",
        f"recognized-partial instructions={','.join(project.partially_modeled_instruction_names) if project.partially_modeled_instruction_names else 'none'}",
        f"unmodeled instructions={','.join(project.unknown_instruction_names) if project.unknown_instruction_names else 'none'}",
        f"indirect-addressed RLL rungs={state['indirect']}",
    )
    incomplete = any(
        state[key]
        for key in (
            "unsupported_types", "protected_routines", "protected_aois", "unmodeled_aois",
            "unresolved_aoi_calls", "unmodeled_branches", "unmodeled_st", "indirect", "no_logic",
            "incomplete_instruction", "partial_instructions",
        )
    )
    checks.append(
        StaticCheck(
            id="LOGIC_SEMANTIC_COVERAGE",
            status=(
                StaticCheckStatus.NOT_PROVEN if state["no_logic"]
                else StaticCheckStatus.WARN if incomplete
                else StaticCheckStatus.PASS
            ),
            summary=(
                "No executable logic was normalized; PLC logic semantic coverage cannot be proven."
                if state["no_logic"]
                else "V2 normalized the supported RLL/ST/AOI/branch semantics, but one or more logic regions remain partial or opaque."
                if incomplete
                else "All discovered supported RLL/ST/AOI/branch logic passed the V2 deterministic semantic model."
            ),
            evidence=coverage_evidence,
        )
    )

    checks.append(
        StaticCheck(
            id="BRANCH_DEPENDENCY_SEMANTICS",
            status=StaticCheckStatus.WARN if state["unmodeled_branches"] else StaticCheckStatus.PASS,
            summary=(
                f"Modeled {project.branch_rung_semantic_count}/{project.branch_rung_total} branched RLL rung(s) with output-specific boolean paths; "
                f"{state['unmodeled_branches']} remain withheld from derived dependencies/FAT."
                if project.branch_rung_total
                else "No Rockwell neutral-text branch syntax was detected in parsed RLL rungs."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="STRUCTURED_TEXT_SEMANTICS",
            status=StaticCheckStatus.WARN if state["unmodeled_st"] else StaticCheckStatus.PASS,
            summary=(
                f"Modeled {project.st_statement_semantic_count}/{project.st_statement_total} Structured Text statement(s) with source-traceable reads/writes/control dependencies."
                if project.st_statement_total
                else "No Structured Text statements require analysis."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="AOI_INTERNAL_LOGIC",
            status=(
                StaticCheckStatus.NOT_PROVEN if state["unmodeled_aois"] or state["protected_aois"]
                else StaticCheckStatus.PASS
            ),
            summary=(
                f"Normalized {project.aoi_internal_modeled_count}/{project.aoi_internal_total} Add-On Instruction internal bodies."
                if project.aoi_internal_total
                else "No Add-On Instruction definitions require internal-body analysis."
            ),
            evidence=tuple(aoi.name for aoi in project.aois),
        )
    )
    checks.append(
        StaticCheck(
            id="AOI_CALL_BINDING",
            status=StaticCheckStatus.WARN if state["unresolved_aoi_calls"] else StaticCheckStatus.PASS,
            summary=(
                f"Proved directional parameter binding for {project.aoi_call_bound_count}/{project.aoi_call_total} AOI invocation(s) using backing-tag type plus exported parameter order."
                if project.aoi_call_total
                else "No AOI invocations require call-interface binding."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="INDIRECT_ADDRESSING_SEMANTICS",
            status=StaticCheckStatus.WARN if state["indirect"] else StaticCheckStatus.PASS,
            summary=(
                f"{state['indirect']} RLL rung(s) use variable array subscripts; index references are retained, but output-path derivation is withheld until an index value is fixed."
                if state["indirect"]
                else "No variable array subscripts were detected in parsed RLL rungs."
            ),
        )
    )

    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    checks.append(
        StaticCheck(
            id="DEPENDENCY_GRAPH",
            status=StaticCheckStatus.PASS if dependency_edges else StaticCheckStatus.WARN,
            summary=f"Dependency graph contains {len(graph.edges)} edges, including {len(dependency_edges)} deterministic DEPENDS_ON edges.",
        )
    )

    traceable = bool(fat_tests) and all(test.source.artifact and test.source.routine for test in fat_tests)
    checks.append(
        StaticCheck(
            id="FAT_TEST_TRACEABILITY",
            status=StaticCheckStatus.PASS if traceable else StaticCheckStatus.WARN,
            summary=(
                f"{len(fat_tests)} deterministic static FAT candidate(s) are traceable to source logic."
                if traceable
                else "No source-traceable deterministic FAT candidates were generated."
            ),
        )
    )
    checks.append(
        StaticCheck(
            id="SIMULATOR_EXECUTION",
            status=StaticCheckStatus.NOT_PROVEN,
            summary="Simulator execution is not part of PLC V2 static analysis; no dynamic machine behavior is claimed as verified.",
        )
    )
    return checks


def analyze_rockwell_l5x(path) -> PLCEngineeringResult:
    project = parse_full_project_l5x(path)
    _populate_structured_unknown_instruction_names(project)
    _normalize_vendor_instruction_aliases(project)
    apply_v2_semantics(project)
    graph = build_dependency_graph(project)
    fat_tests = generate_fat_tests(project)
    checks = static_verify(project, graph, fat_tests)
    state = _coverage_state(project)

    incomplete = any(
        state[key]
        for key in (
            "unsupported_types", "protected_routines", "protected_aois", "unmodeled_aois",
            "unresolved_aoi_calls", "unmodeled_branches", "unmodeled_st", "indirect", "no_logic",
            "incomplete_instruction", "partial_instructions",
        )
    )
    outcome = PLCOutcome.PARTIALLY_VERIFIED if incomplete else PLCOutcome.STATICALLY_VERIFIED

    limitations = [
        "PLC V2 performs deterministic static analysis only; it does not execute Studio 5000, Logix Echo, or a real controller.",
        "FAT cases are engineering test candidates, not PASS results, until an execution backend observes expected behavior.",
        "The analyzer does not infer safety integrity level, required timing, or machine requirements that are absent from the project.",
        *project.warnings,
    ]
    if state["no_logic"]:
        limitations.append("No executable supported logic was normalized; controller behavior remains NOT_PROVEN.")
    if state["unmodeled_aois"]:
        limitations.append(
            f"{state['unmodeled_aois']} Add-On Instruction definition(s) contain unsupported, protected, or partial internal logic; their behavior remains NOT_PROVEN."
        )
    if state["unresolved_aoi_calls"]:
        limitations.append(
            f"{state['unresolved_aoi_calls']} AOI invocation(s) could not be directionally bound to an exported backing tag/interface and remain reference-only."
        )
    if state["unmodeled_branches"]:
        limitations.append(
            f"{state['unmodeled_branches']} branched RLL rung(s) contain logic outside the bounded XIC/XIO/OTE/OTL/OTU boolean-path model; derived output dependencies/FAT are withheld for those rungs."
        )
    if state["unmodeled_st"]:
        limitations.append(
            f"{state['unmodeled_st']} Structured Text statement(s) contain control/call semantics outside the bounded V2 ST model and remain PARTIAL."
        )
    if state["indirect"]:
        limitations.append(
            f"{state['indirect']} RLL rung(s) use variable array subscripts; index references are retained, but V2 withholds output-path FAT until an index value is fixed."
        )
    if project.partially_modeled_instruction_names:
        limitations.append(
            "Recognized Rockwell instructions remain directionally PARTIAL and do not contribute to fully proven instruction coverage: "
            + ", ".join(project.partially_modeled_instruction_names)
        )
    return PLCEngineeringResult(
        outcome=outcome,
        project=project,
        graph=graph,
        fat_tests=fat_tests,
        static_checks=checks,
        limitations=limitations,
    )
