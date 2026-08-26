from __future__ import annotations

import hashlib
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
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.rockwell_l5x import parse_full_project_l5x


_SIMPLE_FAT_INSTRUCTIONS = {"XIC", "XIO", "OTE", "OTL", "OTU"}
_OUTPUT_INSTRUCTIONS = {"OTE", "OTL", "OTU"}
_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "
_NORMALIZED_VENDOR_INSTRUCTIONS = {"GSV", "SSV", "MOVE"}
_TIMER_COUNTER_INSTRUCTIONS = {"TON", "TOF", "RTO", "CTU", "CTD"}
_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
)
_SUBSCRIPT = re.compile(r"\[([^\]]+)\]")


def _has_neutral_text_branch(text: str) -> bool:
    """Detect Rockwell neutral-text branch brackets outside instruction operands.

    Array subscripts such as ``Inputs[0]`` occur inside instruction parentheses and
    are not branch syntax. V1 still withholds per-output dependencies for true
    branch groups until branch-path semantics are represented explicitly.
    """

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
    """Repair parser tokens that joined subtraction expressions with a hyphen.

    Logix tag identifiers do not use ``-`` as an identifier character; in compact
    expressions such as ``Source-Offset`` it is an arithmetic operator. The initial
    parser kept the original token for provenance, and this analysis pass expands it
    into the individual source references before graph construction.
    """

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


def _normalize_vendor_instruction_aliases(project: CanonicalPLCProject) -> None:
    """Normalize bounded vendor semantics before graph/test generation.

    Rockwell v36 renamed MOV to MOVE. GSV writes its destination operand and SSV
    reads its source operand. OSR/OSF have a storage bit plus a distinct output bit.
    Timer/counter neutral text can carry tag-based preset/accumulator operands, which
    are also represented explicitly here. AOI bodies are not normalized in V1, so
    AOI call operands are retained as references while directional reads/writes are
    withheld rather than inferred from an incomplete interface model.
    """

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
                for ref in _operand_refs(args[0]) if args else ():
                    reads.add(ref)
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
            # The parser's original AOI parameter zip cannot safely distinguish the
            # backing instance operand and implicit/system parameters. Until AOI
            # interfaces/bodies are normalized, preserve operands only as references.
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

    retained_warnings = [
        warning
        for warning in project.warnings
        if not warning.startswith(_UNKNOWN_WARNING_PREFIX)
    ]
    if remaining_unknown:
        retained_warnings.append(
            f"{_UNKNOWN_WARNING_PREFIX}{', '.join(remaining_unknown)}"
        )
    project.warnings = retained_warnings


def _simple_boolean_output(rung: PLCRung) -> tuple[str, str] | None:
    """Return the single terminal output instruction for a proven-simple rung.

    DEPENDS_ON is intentionally stronger than READS/WRITES. V1 only derives it for
    a straight-line boolean rung made of XIC/XIO contacts followed by exactly one
    terminal OTE/OTL/OTU. Complex instruction sequencing, branches, and indirect
    addressing keep their source facts but do not receive speculative dependencies.
    """

    if (
        _has_neutral_text_branch(rung.text)
        or _rung_has_variable_array_subscript(rung)
        or not rung.instructions
    ):
        return None
    names = [instruction.name.upper() for instruction in rung.instructions]
    if not set(names).issubset(_SIMPLE_FAT_INSTRUCTIONS):
        return None
    output_indexes = [
        index for index, name in enumerate(names) if name in _OUTPUT_INSTRUCTIONS
    ]
    if len(output_indexes) != 1:
        return None
    output_index = output_indexes[0]
    if output_index != len(rung.instructions) - 1:
        return None
    if any(name not in {"XIC", "XIO"} for name in names[:output_index]):
        return None
    instruction = rung.instructions[output_index]
    output = _first_operand_ref(instruction.arguments)
    if output is None:
        return None
    return instruction.name.upper(), output


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

        simple_output = _simple_boolean_output(rung)
        if simple_output is None:
            continue
        _, output = simple_output
        for dependency in rung.reads:
            add(output, dependency, "DEPENDS_ON", rung.id)

    return PLCDependencyGraph(
        edges=edges,
        unknown_instruction_names=list(project.unknown_instruction_names),
    )


def generate_fat_tests(project: CanonicalPLCProject) -> list[FATTestCase]:
    """Generate conservative test candidates only for proven-simple boolean RLL."""

    tests: list[FATTestCase] = []
    for rung in project.rungs:
        simple_output = _simple_boolean_output(rung)
        if simple_output is None:
            continue
        output_instruction, output = simple_output

        preconditions: dict[str, bool] = {}
        contradictory = False
        for instruction in rung.instructions[:-1]:
            name = instruction.name.upper()
            tag = _first_operand_ref(instruction.arguments)
            if tag is None:
                continue
            required = name == "XIC"
            if tag in preconditions and preconditions[tag] != required:
                contradictory = True
                break
            preconditions[tag] = required
        if contradictory or not preconditions:
            continue

        if output_instruction == "OTE":
            expected = f"{output}=TRUE while the evaluated rung-in condition is TRUE"
        elif output_instruction == "OTL":
            expected = (
                f"{output}=TRUE (latched) after the evaluated rung-in condition becomes TRUE"
            )
        else:
            expected = (
                f"{output}=FALSE (unlatched) after the evaluated rung-in condition becomes TRUE"
            )
        digest = hashlib.sha1(
            f"{rung.id}:{output_instruction}:{output}".encode("utf-8")
        ).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-RLL-{digest}",
                title=(
                    f"Exercise {output} logic at "
                    f"{rung.program}/{rung.routine} rung {rung.number}"
                ),
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

    unsupported_types = sorted(
        {
            routine.routine_type
            for routine in project.routines
            if routine.routine_type != "RLL"
        }
    )
    protected_routine_count = sum(
        1 for routine in project.routines if routine.source_protected
    )
    protected_aoi_count = sum(1 for aoi in project.aois if aoi.source_protected)
    unparsed_aoi_count = len(project.aois)
    branch_rung_count = sum(
        1 for rung in project.rungs if _has_neutral_text_branch(rung.text)
    )
    indirect_rung_count = sum(
        1 for rung in project.rungs if _rung_has_variable_array_subscript(rung)
    )
    instruction_count = sum(len(rung.instructions) for rung in project.rungs)
    no_parsed_logic = not project.rungs or instruction_count == 0

    coverage_evidence = (
        f"semantic instructions={project.instruction_semantic_count}/{project.instruction_total}",
        f"non-RLL types={','.join(unsupported_types) if unsupported_types else 'none'}",
        f"protected routines={protected_routine_count}",
        f"protected AOIs={protected_aoi_count}",
        f"AOI internal bodies modeled=0/{unparsed_aoi_count}",
        f"branched RLL rungs={branch_rung_count}",
        f"indirect-addressed RLL rungs={indirect_rung_count}",
    )
    if no_parsed_logic:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.NOT_PROVEN,
                summary=(
                    "No executable RLL instructions were parsed; "
                    "PLC logic semantic coverage cannot be proven."
                ),
                evidence=coverage_evidence,
            )
        )
    elif (
        unsupported_types
        or protected_routine_count
        or unparsed_aoi_count
        or branch_rung_count
        or indirect_rung_count
        or project.instruction_semantic_coverage < 1.0
    ):
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.WARN,
                summary=(
                    f"Deterministic instruction semantic coverage is "
                    f"{project.instruction_semantic_coverage:.1%}; unmodeled, "
                    "protected, AOI-body, branch-path, or indirect-addressing logic "
                    "is not treated as fully proven."
                ),
                evidence=coverage_evidence,
            )
        )
    else:
        checks.append(
            StaticCheck(
                id="LOGIC_SEMANTIC_COVERAGE",
                status=StaticCheckStatus.PASS,
                summary=(
                    "All parsed RLL instructions and supported dependency paths "
                    "are covered by the V1 deterministic semantic model."
                ),
                evidence=coverage_evidence,
            )
        )

    checks.append(
        StaticCheck(
            id="BRANCH_DEPENDENCY_SEMANTICS",
            status=(
                StaticCheckStatus.WARN
                if branch_rung_count
                else StaticCheckStatus.PASS
            ),
            summary=(
                f"{branch_rung_count} branched RLL rung(s) retain rung-level "
                "reads/writes, but per-output DEPENDS_ON edges and FAT candidates "
                "are withheld to avoid false cross-branch dependencies."
                if branch_rung_count
                else "No Rockwell neutral-text branch syntax was detected in parsed RLL rungs."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="INDIRECT_ADDRESSING_SEMANTICS",
            status=(
                StaticCheckStatus.WARN
                if indirect_rung_count
                else StaticCheckStatus.PASS
            ),
            summary=(
                f"{indirect_rung_count} RLL rung(s) use variable array subscripts; "
                "index references are retained, but per-output DEPENDS_ON edges and "
                "FAT candidates are withheld until an index value is fixed."
                if indirect_rung_count
                else "No variable array subscripts were detected in parsed RLL rungs."
            ),
        )
    )

    if unparsed_aoi_count:
        checks.append(
            StaticCheck(
                id="AOI_INTERNAL_LOGIC",
                status=StaticCheckStatus.NOT_PROVEN,
                summary=(
                    f"{unparsed_aoi_count} Add-On Instruction definition(s) were inventoried, "
                    "but their internal routines are not normalized in PLC V1."
                ),
                evidence=tuple(aoi.name for aoi in project.aois),
            )
        )
    else:
        checks.append(
            StaticCheck(
                id="AOI_INTERNAL_LOGIC",
                status=StaticCheckStatus.PASS,
                summary="No Add-On Instruction definitions require internal-body analysis.",
            )
        )

    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    checks.append(
        StaticCheck(
            id="DEPENDENCY_GRAPH",
            status=(
                StaticCheckStatus.PASS
                if project.rungs and dependency_edges
                else StaticCheckStatus.WARN
            ),
            summary=(
                f"Dependency graph contains {len(graph.edges)} edges, including "
                f"{len(dependency_edges)} proven simple-boolean tag dependencies."
            ),
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
                else "No traceable proven-simple RLL FAT test candidates were generated."
            ),
        )
    )

    checks.append(
        StaticCheck(
            id="SIMULATOR_EXECUTION",
            status=StaticCheckStatus.NOT_PROVEN,
            summary=(
                "Simulator execution is not part of Rockwell PLC V1; "
                "no dynamic machine behavior is claimed as verified."
            ),
        )
    )
    return checks


def analyze_rockwell_l5x(path) -> PLCEngineeringResult:
    project = parse_full_project_l5x(path)
    _populate_structured_unknown_instruction_names(project)
    _normalize_vendor_instruction_aliases(project)
    graph = build_dependency_graph(project)
    fat_tests = generate_fat_tests(project)
    checks = static_verify(project, graph, fat_tests)

    unsupported_types = {
        routine.routine_type
        for routine in project.routines
        if routine.routine_type != "RLL"
    }
    protected_routine_count = sum(
        1 for routine in project.routines if routine.source_protected
    )
    protected_aoi_count = sum(1 for aoi in project.aois if aoi.source_protected)
    unparsed_aoi_count = len(project.aois)
    branch_rung_count = sum(
        1 for rung in project.rungs if _has_neutral_text_branch(rung.text)
    )
    indirect_rung_count = sum(
        1 for rung in project.rungs if _rung_has_variable_array_subscript(rung)
    )
    incomplete_semantics = project.instruction_semantic_coverage < 1.0
    instruction_count = sum(len(rung.instructions) for rung in project.rungs)
    no_parsed_logic = not project.rungs or instruction_count == 0

    outcome = (
        PLCOutcome.PARTIALLY_VERIFIED
        if (
            unsupported_types
            or protected_routine_count
            or unparsed_aoi_count
            or branch_rung_count
            or indirect_rung_count
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
        limitations.append(
            "No executable RLL instructions were parsed; controller logic verification remains NOT_PROVEN."
        )
    if unparsed_aoi_count:
        limitations.append(
            f"{unparsed_aoi_count} Add-On Instruction definition(s) are inventoried, but their internal routines and directional call-interface semantics are not normalized in PLC V1; AOI behavior remains NOT_PROVEN."
        )
    if protected_aoi_count:
        limitations.append(
            f"{protected_aoi_count} Add-On Instruction definition(s) contain encoded/protected content."
        )
    if branch_rung_count:
        limitations.append(
            f"{branch_rung_count} branched RLL rung(s) are retained as source facts, but V1 withholds per-output dependencies and FAT candidates until branch-path semantics are modeled."
        )
    if indirect_rung_count:
        limitations.append(
            f"{indirect_rung_count} RLL rung(s) use variable array subscripts; index references are retained, but V1 withholds per-output dependencies and FAT candidates until an index value is fixed."
        )
    return PLCEngineeringResult(
        outcome=outcome,
        project=project,
        graph=graph,
        fat_tests=fat_tests,
        static_checks=checks,
        limitations=limitations,
    )
