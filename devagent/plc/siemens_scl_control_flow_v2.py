from __future__ import annotations

from dataclasses import replace
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from devagent.plc.analysis import build_dependency_graph
from devagent.plc.models import (
    PLCBooleanTerm,
    PLCEngineeringResult,
    PLCLogicPath,
    PLCOutcome,
    PLCOutputLogic,
    PLCSemanticState,
)
from devagent.plc.production_models import RequirementStatus, RequirementVerification
from devagent.plc.production_utils import explicit_bool
from devagent.plc import siemens_tia_v1 as _v1


_INSTALLED = False
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\s*;?\s*$", re.IGNORECASE)
_END_IF = re.compile(r"^\s*END_IF\s*;?\s*$", re.IGNORECASE)
_UNSUPPORTED_CONTROL = re.compile(
    r"^\s*(CASE|FOR|WHILE|REPEAT|END_CASE|END_FOR|END_WHILE|UNTIL)\b",
    re.IGNORECASE,
)
_ORIGIN = re.compile(r"^SIEMENS_SCL_IF_CHAIN:(?P<start>\d+)-(?P<end>\d+)$")


def _line_number(statement) -> int | None:
    raw = statement.source.line
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def _merge_terms(left: dict[str, bool], right: dict[str, bool]) -> dict[str, bool] | None:
    merged = dict(left)
    folded = {key.casefold(): key for key in merged}
    for key, value in right.items():
        existing = folded.get(key.casefold())
        if existing is not None:
            if merged[existing] != value:
                return None
            continue
        merged[key] = value
        folded[key.casefold()] = key
    return merged


def _dedupe_paths(paths: list[dict[str, bool]]) -> list[dict[str, bool]] | None:
    unique: list[dict[str, bool]] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for path in paths:
        key = tuple(sorted(((name.casefold(), bool(value)) for name, value in path.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if len(unique) > 32 or any(len(path) > 16 for path in unique):
        return None
    return unique


def _and_dnf(left: list[dict[str, bool]] | None, right: list[dict[str, bool]] | None) -> list[dict[str, bool]] | None:
    if left is None or right is None:
        return None
    combined: list[dict[str, bool]] = []
    for a in left:
        for b in right:
            merged = _merge_terms(a, b)
            if merged is not None:
                combined.append(merged)
    return _dedupe_paths(combined)


def _exclusive_guard(guards: list[object], index: int, *, is_else: bool) -> list[dict[str, bool]] | None:
    paths: list[dict[str, bool]] | None = [{}]
    for prior in guards[:index]:
        paths = _and_dnf(paths, _v1._dnf(prior, True))
    if paths is None:
        return None
    if is_else:
        return paths
    return _and_dnf(paths, _v1._dnf(guards[index], False))


def _group_scl_statements(project):
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for statement in project.logic_statements:
        if statement.language != "SCL" or _line_number(statement) is None:
            continue
        key = (
            (statement.source.program or statement.owner_name or "").casefold(),
            (statement.source.routine or statement.routine or "").casefold(),
        )
        groups[key].append(statement)
    for values in groups.values():
        values.sort(key=lambda item: (_line_number(item) or 0, item.id))
    return groups


def _collect_chain(statements: list, start: int):
    first = statements[start]
    if _IF.match(first.text) is None:
        return None
    branches: list[dict[str, object]] = []
    first_match = _IF.match(first.text)
    assert first_match is not None
    branches.append({"guard": first_match.group("expr"), "statements": []})
    controls = [first]
    cursor = start + 1
    nested = False
    saw_else = False
    while cursor < len(statements):
        statement = statements[cursor]
        text = statement.text.strip()
        if _IF.match(text):
            nested = True
            break
        if _UNSUPPORTED_CONTROL.match(text):
            nested = True
            break
        match = _ELSIF.match(text)
        if match:
            if saw_else:
                return None
            branches.append({"guard": match.group("expr"), "statements": []})
            controls.append(statement)
            cursor += 1
            continue
        if _ELSE.match(text):
            if saw_else:
                return None
            saw_else = True
            branches.append({"guard": None, "statements": []})
            controls.append(statement)
            cursor += 1
            continue
        if _END_IF.match(text):
            controls.append(statement)
            if nested or not saw_else:
                return None
            return {
                "start_index": start,
                "end_index": cursor,
                "branches": branches,
                "controls": controls,
                "all_statements": statements[start : cursor + 1],
            }
        branches[-1]["statements"].append(statement)
        cursor += 1
    return None


def _analyze_chain(chain) -> dict[str, tuple[PLCLogicPath, ...]] | None:
    branches = chain["branches"]
    if not branches or branches[-1]["guard"] is not None:
        return None

    guard_asts: list[object] = []
    for branch in branches[:-1]:
        ast = _v1._parse_bool_ast(str(branch["guard"]))
        if ast is None or _v1._dnf(ast) is None or _v1._dnf(ast, True) is None:
            return None
        guard_asts.append(ast)

    per_branch: list[dict[str, object]] = []
    output_sets: list[set[str]] = []
    all_outputs: dict[str, str] = {}
    for branch in branches:
        assignments: dict[str, tuple[str, object, object]] = {}
        for statement in branch["statements"]:
            if statement.calls:
                return None
            match = _v1._ASSIGNMENT.match(statement.text)
            if match is None:
                return None
            lhs = _v1._lhs_ref(match.group("lhs"))
            if lhs is None:
                return None
            key = lhs.casefold()
            if key in assignments:
                # Multiple writes in one branch require statement-order semantics.
                return None
            rhs = match.group("rhs").strip()
            rhs_ast = _v1._parse_bool_ast(rhs)
            if rhs_ast is None or _v1._dnf(rhs_ast) is None:
                return None
            assignments[key] = (lhs, rhs_ast, statement)
            all_outputs.setdefault(key, lhs)
        if not assignments:
            return None
        output_sets.append(set(assignments))
        per_branch.append(assignments)

    # A final value theorem requires every output to be assigned exactly once in
    # every IF/ELSIF/ELSE branch. Missing assignments can retain prior state.
    if any(output_set != output_sets[0] for output_set in output_sets[1:]):
        return None
    outputs = output_sets[0]
    if not outputs:
        return None

    # Keep V2 acyclic: a chain must not read any output that it also writes.
    written = {name.casefold() for name in outputs}
    for ast in guard_asts:
        for path in _v1._dnf(ast) or []:
            if any(name.casefold() in written for name in path):
                return None
    for assignments in per_branch:
        for _, rhs_ast, _ in assignments.values():
            for path in _v1._dnf(rhs_ast) or []:
                if any(name.casefold() in written for name in path):
                    return None

    result: dict[str, tuple[PLCLogicPath, ...]] = {}
    for output_key in sorted(outputs):
        true_paths: list[dict[str, bool]] = []
        for index, assignments in enumerate(per_branch):
            is_else = index == len(per_branch) - 1
            guard_paths = _exclusive_guard(guard_asts, index, is_else=is_else)
            _, rhs_ast, _ = assignments[output_key]
            rhs_paths = _v1._dnf(rhs_ast)
            combined = _and_dnf(guard_paths, rhs_paths)
            if combined is None:
                return None
            true_paths.extend(combined)
        deduped = _dedupe_paths(true_paths)
        if deduped is None:
            return None
        result[all_outputs[output_key]] = tuple(
            PLCLogicPath(
                tuple(
                    PLCBooleanTerm(tag=name, required=value)
                    for name, value in sorted(path.items(), key=lambda item: item[0].casefold())
                )
            )
            for path in deduped
        )
    return result


def _upgrade_if_chains(project) -> int:
    replacements: dict[str, object] = {}
    new_output_logic: list[PLCOutputLogic] = []
    modeled_chains = 0

    for statements in _group_scl_statements(project).values():
        cursor = 0
        while cursor < len(statements):
            if _IF.match(statements[cursor].text) is None:
                cursor += 1
                continue
            chain = _collect_chain(statements, cursor)
            if chain is None:
                cursor += 1
                continue
            modeled = _analyze_chain(chain)
            if modeled is None:
                cursor = int(chain["end_index"]) + 1
                continue

            start_line = _line_number(chain["all_statements"][0])
            end_line = _line_number(chain["all_statements"][-1])
            assert start_line is not None and end_line is not None
            origin = f"SIEMENS_SCL_IF_CHAIN:{start_line}-{end_line}"
            for statement in chain["all_statements"]:
                replacements[statement.id] = replace(statement, semantic_state=PLCSemanticState.FULL)

            source = chain["all_statements"][0].source
            for output, paths in modeled.items():
                digest = hashlib.sha1(
                    f"{source.program}:{source.routine}:{start_line}:{end_line}:{output}".encode("utf-8")
                ).hexdigest()[:14]
                new_output_logic.append(
                    PLCOutputLogic(
                        id=f"SIEMENS-IF2-{digest}",
                        output_tag=output,
                        instruction="ASSIGN_BOOL",
                        paths=paths,
                        source=source,
                        language="SCL",
                        origin=origin,
                        semantic_state=PLCSemanticState.FULL,
                    )
                )
            modeled_chains += 1
            cursor = int(chain["end_index"]) + 1

    if not modeled_chains:
        return 0

    project.logic_statements = [replacements.get(item.id, item) for item in project.logic_statements]
    existing = {item.id for item in project.output_logic}
    project.output_logic.extend(item for item in new_output_logic if item.id not in existing)
    project.instruction_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements
    )
    scl = [item for item in project.logic_statements if item.language == "SCL"]
    project.st_statement_total = len(scl)
    project.st_statement_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in scl
    )
    project.partially_modeled_instruction_names = sorted(
        {item.language for item in project.logic_statements if item.semantic_state is not PLCSemanticState.FULL}
    )
    project.metadata = replace(
        project.metadata,
        schema_revision="SIEMENS-TIA-EXPORT-V2",
    )
    return modeled_chains


def _if_chain_ranges(project, output: str):
    result = []
    for logic in project.output_logic:
        if logic.output_tag.casefold() != output.casefold():
            continue
        match = _ORIGIN.match(logic.origin)
        if match is None:
            continue
        result.append((logic, int(match.group("start")), int(match.group("end"))))
    return result


def _writers_belong_to_single_chain(project, output: str, logic) -> tuple[bool, tuple[str, ...]]:
    match = _ORIGIN.match(logic.origin)
    if match is None:
        return False, ()
    start = int(match.group("start"))
    end = int(match.group("end"))
    writers = []
    outside = []
    for statement in project.logic_statements:
        if not any(ref.casefold() == output.casefold() for ref in statement.writes):
            continue
        writers.append(statement.id)
        line = _line_number(statement)
        same_routine = (
            (statement.source.program or "").casefold() == (logic.source.program or "").casefold()
            and (statement.source.routine or "").casefold() == (logic.source.routine or "").casefold()
        )
        if line is None or not same_routine or not (start <= line <= end):
            outside.append(statement.id)
    return bool(writers) and not outside, tuple(writers)


def siemens_capability_profile_v2(project) -> dict[str, object]:
    profile = dict(_v1._ORIGINAL_CAPABILITY(project) if hasattr(_v1, "_ORIGINAL_CAPABILITY") else _v1.siemens_capability_profile(project))
    chains = {
        logic.origin
        for logic in project.output_logic
        if _ORIGIN.match(logic.origin)
    }
    profile["schema"] = "devagent-siemens-tia-capability-v2"
    profile["if_chain_models"] = len(chains)
    profile["if_chain_output_logic"] = sum(1 for logic in project.output_logic if _ORIGIN.match(logic.origin))
    profile["bounded_control_flow"] = "single-level complete IF/ELSIF/ELSE Boolean assignment chains"
    return profile


def analyze_siemens_tia_v2(path: Path) -> PLCEngineeringResult:
    base = _v1._ORIGINAL_ANALYZER(path) if hasattr(_v1, "_ORIGINAL_ANALYZER") else _v1.analyze_siemens_tia(path)
    project = base.project
    modeled_chains = _upgrade_if_chains(project)
    if not modeled_chains:
        return base

    graph = build_dependency_graph(project)
    fat_tests = _v1._siemens_fat_tests(project)
    checks = _v1._siemens_checks(project, graph, fat_tests)
    profile = siemens_capability_profile_v2(project)
    outcome = PLCOutcome.STATICALLY_VERIFIED if profile["static_contract"] == "COMPLETE" else PLCOutcome.PARTIALLY_VERIFIED
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED

    limitations = []
    for item in base.limitations:
        item = item.replace("Siemens V1", "Siemens V2")
        item = item.replace(
            "Only bounded top-level SCL assignment/Boolean dataflow is eligible for static proof in V1. IF/CASE/loop/call semantics and LAD/FBD/GRAPH/STL XML networks remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
            "Siemens V2 adds a bounded theorem for single-level complete IF/ELSIF/ELSE Boolean assignment chains. Nested control flow, CASE/loops, calls, complex expressions, and LAD/FBD/GRAPH/STL XML networks remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
        )
        limitations.append(item)
    limitations.append(
        f"Siemens V2 deterministically modeled {modeled_chains} complete single-level IF/ELSIF/ELSE chain(s); missing-ELSE, missing-output, cyclic/self-referential, nested, call-bearing, or oversized chains remain fail-closed."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _v2_verify_requirement(previous, requirement, engineering, evidence, tests):
    base = previous(requirement, engineering, evidence, tests)
    if base.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return base

    project = engineering.project
    matched = list(base.matched_tags)
    outputs = []
    for logic in project.output_logic:
        if _ORIGIN.match(logic.origin) is None:
            continue
        explicit = next((tag for tag in matched if tag.casefold() == logic.output_tag.casefold()), None)
        if explicit is not None and explicit_bool(requirement.text, explicit) is not None:
            outputs.append(logic)
    if len(outputs) != 1:
        return base
    logic = outputs[0]
    safe_writers, writer_ids = _writers_belong_to_single_chain(project, logic.output_tag, logic)
    if not safe_writers:
        return base

    output_text = next(tag for tag in matched if tag.casefold() == logic.output_tag.casefold())
    expected = explicit_bool(requirement.text, output_text)
    assert expected is not None
    assignment = {
        tag: value
        for tag in matched
        if tag.casefold() != logic.output_tag.casefold()
        for value in [explicit_bool(requirement.text, tag)]
        if value is not None
    }
    if not assignment:
        return base

    from devagent.plc import siemens_integration_v1 as _integration

    truth = _integration._siemens_bool_truth(logic, assignment, expected)
    combined = tuple(dict.fromkeys([*base.evidence_ids, logic.id, *writer_ids]))
    linked = tuple(
        test.id
        for test in tests
        if test.output_tag.casefold() == logic.output_tag.casefold()
        and all(test.preconditions.get(key) == value for key, value in assignment.items())
    )
    if truth == "PROVEN":
        return RequirementVerification(
            requirement.id,
            RequirementStatus.STATICALLY_VERIFIED,
            f"Specified Boolean conditions deterministically imply {logic.output_tag}={'TRUE' if expected else 'FALSE'} in the Siemens V2 complete IF/ELSIF/ELSE assignment theorem; runtime machine behavior still requires FAT when policy requires dynamic proof.",
            combined,
            tuple(matched),
            linked,
        )
    if truth == "CONFLICT":
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Specified Boolean conditions make required {logic.output_tag}={'TRUE' if expected else 'FALSE'} impossible in the Siemens V2 complete IF/ELSIF/ELSE assignment theorem.",
            combined,
            tuple(matched),
        )
    return replace(base, evidence_ids=combined)


def _v2_detect_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    if str(engineering.project.metadata.vendor).casefold() != "siemens":
        return risks
    safe_outputs = set()
    for logic in engineering.project.output_logic:
        if _ORIGIN.match(logic.origin) is None:
            continue
        safe, _ = _writers_belong_to_single_chain(engineering.project, logic.output_tag, logic)
        same_output_chains = _if_chain_ranges(engineering.project, logic.output_tag)
        if safe and len(same_output_chains) == 1:
            safe_outputs.add(logic.output_tag.casefold())
    return [
        risk
        for risk in risks
        if not (
            risk.category == "MULTIPLE_WRITERS"
            and any(output in risk.title.casefold() for output in safe_outputs)
        )
    ]


def _v2_semantic_section(previous, project) -> str:
    base = previous(project)
    if str(project.metadata.vendor).casefold() != "siemens":
        return base
    profile = siemens_capability_profile_v2(project)
    base = base.replace("Siemens V1 separates", "Siemens V2 separates")
    base = base.replace("### Explicit Siemens V1 Boundaries", "### Explicit Siemens V2 Boundaries")
    base = base.replace(
        "- SCL inside IF/ELSIF/CASE/FOR/WHILE/REPEAT, block-call semantics, and complex expressions remain PARTIAL until a dedicated theorem models their execution semantics.",
        "- Single-level complete SCL IF/ELSIF/ELSE chains may receive deterministic Boolean final-value proof when every branch assigns the same outputs exactly once and all guards/RHS expressions fit the bounded Boolean grammar.",
    )
    insertion = (
        "### Siemens V2 Bounded Control-Flow Theorem\n\n"
        f"- Modeled complete IF/ELSIF/ELSE chains: **{profile['if_chain_models']}**\n"
        f"- IF-chain Boolean output theorem objects: **{profile['if_chain_output_logic']}**\n"
        "- ELSIF guards are modeled as mutually exclusive: prior branch conditions must be FALSE before the ELSIF condition can select the branch.\n"
        "- Missing ELSE/output assignments, nested controls, CASE/loops, calls, self/cyclic output dependencies, indirect addressing, or theorem-size overflow remain PARTIAL and require engineer FAT.\n"
        "- This is static source semantics only; DevAgent still does not execute PLCSIM, HIL, or a real PLC.\n\n"
    )
    marker = "### Explicit Siemens V2 Boundaries"
    if marker in base and "### Siemens V2 Bounded Control-Flow Theorem" not in base:
        base = base.replace(marker, insertion + marker, 1)
    return base


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration

    # Preserve immutable V1 entry points for the V2 wrapper and for direct
    # qualification of fail-closed behavior.
    if not hasattr(_v1, "_ORIGINAL_ANALYZER"):
        _v1._ORIGINAL_ANALYZER = _v1.analyze_siemens_tia
    if not hasattr(_v1, "_ORIGINAL_CAPABILITY"):
        _v1._ORIGINAL_CAPABILITY = _v1.siemens_capability_profile

    previous_verify = _integration._siemens_verify_requirement
    previous_risks = _integration._siemens_detect_risks
    previous_section = _integration._siemens_semantic_section
    previous_findings = _integration._siemens_findings
    previous_evidence = _integration._siemens_evidence_index

    _v1.analyze_siemens_tia = analyze_siemens_tia_v2
    _v1.siemens_capability_profile = siemens_capability_profile_v2
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v2
    _integration.siemens_capability_profile = siemens_capability_profile_v2

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v2_verify_requirement(previous_verify, requirement, engineering, evidence, tests)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v2_detect_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def semantic_section(project):
        return _v2_semantic_section(previous_section, project)

    def findings(engineering, valid_evidence_ids):
        items = previous_findings(engineering, valid_evidence_ids)
        if str(engineering.project.metadata.vendor).casefold() != "siemens":
            return items
        updated = []
        for item in items:
            summary = item.summary.replace("Siemens V1", "Siemens V2").replace("under the V1 contract", "under the V2 contract")
            recommendation = item.recommendation.replace(
                "Keep IF/CASE/loop/call or structured-network regions outside VERIFIED claims until an explicit theorem or engineer-executed FAT evidence covers them.",
                "Keep nested IF, CASE/loop/call, incomplete IF-chain, or structured-network regions outside VERIFIED claims until an explicit theorem or engineer-executed FAT evidence covers them.",
            )
            updated.append(replace(item, summary=summary, recommendation=recommendation))
        return updated

    def evidence_index(engineering):
        items = previous_evidence(engineering)
        if str(engineering.project.metadata.vendor).casefold() != "siemens":
            return items
        return [
            replace(item, summary=item.summary.replace("Siemens TIA V1", "Siemens TIA V2"))
            for item in items
        ]

    _integration._siemens_verify_requirement = verify_requirement
    _integration._siemens_detect_risks = detect_risks
    _integration._siemens_semantic_section = semantic_section
    _integration._siemens_findings = findings
    _integration._siemens_evidence_index = evidence_index
    _INSTALLED = True


__all__ = [
    "analyze_siemens_tia_v2",
    "install",
    "siemens_capability_profile_v2",
]
