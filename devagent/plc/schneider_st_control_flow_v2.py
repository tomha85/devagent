from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

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
from devagent.plc import schneider_control_expert_v1 as _v1


_INSTALLED = False

_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\s*;?\s*$", re.IGNORECASE)
_END_IF = re.compile(r"^\s*END_IF\s*;?\s*$", re.IGNORECASE)
_UNSUPPORTED_OPEN = re.compile(r"^\s*(?P<kind>CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_UNSUPPORTED_CLOSE = re.compile(
    r"^\s*(?P<kind>END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b",
    re.IGNORECASE,
)
_ORIGIN = re.compile(r"^SCHNEIDER_ST_IF_CHAIN:(?P<start>\d+)-(?P<end>\d+)$")
_MAX_PATHS = 32
_MAX_TERMS_PER_PATH = 16
_MAX_ASSIGNMENTS_PER_CHAIN = 64


def _statement_line(statement) -> int | None:
    raw = statement.source.line
    if raw is None:
        return None
    match = re.match(r"^\s*(\d+)", str(raw))
    return int(match.group(1)) if match else None


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
        if len(path) > _MAX_TERMS_PER_PATH:
            return None
        key = tuple(sorted((name.casefold(), bool(value)) for name, value in path.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if len(unique) > _MAX_PATHS:
        return None
    return unique


def _and_dnf(
    left: list[dict[str, bool]] | None,
    right: list[dict[str, bool]] | None,
) -> list[dict[str, bool]] | None:
    if left is None or right is None:
        return None
    combined: list[dict[str, bool]] = []
    for lhs in left:
        for rhs in right:
            merged = _merge_terms(lhs, rhs)
            if merged is not None:
                combined.append(merged)
    return _dedupe_paths(combined)


def _exclusive_guard(
    guard_asts: list[object],
    index: int,
    *,
    is_else: bool,
) -> list[dict[str, bool]] | None:
    paths: list[dict[str, bool]] | None = [{}]
    for prior in guard_asts[:index]:
        paths = _and_dnf(paths, _v1._dnf(prior, True))
    if paths is None:
        return None
    if is_else:
        return paths
    return _and_dnf(paths, _v1._dnf(guard_asts[index], False))


def _split_assignments(text: str) -> list[str]:
    return [chunk.strip() for chunk in text.split(";") if chunk.strip()]


def _parse_branch_assignments(lines: list[tuple[int, str]]):
    assignments: dict[str, tuple[str, object, int]] = {}
    for line_no, text in lines:
        chunks = _split_assignments(text)
        if not chunks:
            return None
        for chunk in chunks:
            if len(assignments) >= _MAX_ASSIGNMENTS_PER_CHAIN:
                return None
            if _v1._CALL.match(chunk):
                return None
            match = _v1._ASSIGNMENT.match(chunk + ";")
            if match is None:
                return None
            lhs = _v1._lhs_ref(match.group("lhs"))
            if lhs is None:
                return None
            folded = lhs.casefold()
            if folded in assignments:
                # Multiple writes in one branch require statement-order semantics.
                return None
            rhs = match.group("rhs").strip()
            rhs_ast = _v1._parse_bool_ast(rhs)
            if rhs_ast is None or _v1._dnf(rhs_ast) is None:
                return None
            assignments[folded] = (lhs, rhs_ast, line_no)
    return assignments or None


def _collect_if_chain(lines: list[str], start: int):
    first = _IF.match(lines[start])
    if first is None:
        return None

    branches: list[dict[str, object]] = [{"guard": first.group("expr"), "lines": []}]
    controls: list[tuple[int, str]] = [(start + 1, lines[start])]
    depth = 1
    nested = False
    unsupported = False
    saw_else = False
    cursor = start + 1

    while cursor < len(lines):
        text = lines[cursor].strip()
        line_no = cursor + 1

        if _IF.match(text):
            depth += 1
            nested = True
            cursor += 1
            continue

        if _END_IF.match(text):
            depth -= 1
            if depth == 0:
                return {
                    "start_index": start,
                    "end_index": cursor,
                    "start_line": start + 1,
                    "end_line": line_no,
                    "branches": branches,
                    "controls": controls,
                    "eligible": not nested and not unsupported and saw_else,
                }
            cursor += 1
            continue

        if depth > 1:
            cursor += 1
            continue

        if _UNSUPPORTED_OPEN.match(text) or _UNSUPPORTED_CLOSE.match(text):
            unsupported = True
            cursor += 1
            continue

        match = _ELSIF.match(text)
        if match:
            if saw_else:
                unsupported = True
            branches.append({"guard": match.group("expr"), "lines": []})
            controls.append((line_no, text))
            cursor += 1
            continue

        if _ELSE.match(text):
            if saw_else:
                unsupported = True
            saw_else = True
            branches.append({"guard": None, "lines": []})
            controls.append((line_no, text))
            cursor += 1
            continue

        if text:
            branches[-1]["lines"].append((line_no, text))
        cursor += 1

    # Unterminated IF: skip to EOF and fail closed.
    return {
        "start_index": start,
        "end_index": len(lines) - 1,
        "start_line": start + 1,
        "end_line": len(lines),
        "branches": branches,
        "controls": controls,
        "eligible": False,
    }


def _skip_unsupported_region(lines: list[str], start: int) -> int:
    """Skip an unsupported CASE/FOR/WHILE/REPEAT region without mining inner IFs."""

    stack: list[str] = []
    cursor = start
    while cursor < len(lines):
        text = lines[cursor].strip()
        open_match = _UNSUPPORTED_OPEN.match(text)
        if open_match:
            stack.append(open_match.group("kind").upper())
            cursor += 1
            continue

        close_match = _UNSUPPORTED_CLOSE.match(text)
        if close_match and stack:
            close = close_match.group("kind").upper()
            expected = {
                "CASE": {"END_CASE"},
                "FOR": {"END_FOR"},
                "WHILE": {"END_WHILE"},
                "REPEAT": {"UNTIL", "END_REPEAT"},
            }[stack[-1]]
            if close in expected:
                stack.pop()
                cursor += 1
                if not stack:
                    return cursor
                continue
        cursor += 1
    return len(lines)


def _analyze_chain(chain):
    if not chain or not chain["eligible"]:
        return None
    branches = chain["branches"]
    if len(branches) < 2 or branches[-1]["guard"] is not None:
        return None

    guard_asts: list[object] = []
    for branch in branches[:-1]:
        guard = branch["guard"]
        if guard is None:
            return None
        ast = _v1._parse_bool_ast(str(guard))
        if ast is None or _v1._dnf(ast) is None or _v1._dnf(ast, True) is None:
            return None
        guard_asts.append(ast)

    per_branch: list[dict[str, tuple[str, object, int]]] = []
    output_sets: list[set[str]] = []
    labels: dict[str, str] = {}
    total_assignments = 0
    for branch in branches:
        assignments = _parse_branch_assignments(branch["lines"])
        if assignments is None:
            return None
        total_assignments += len(assignments)
        if total_assignments > _MAX_ASSIGNMENTS_PER_CHAIN:
            return None
        per_branch.append(assignments)
        output_sets.append(set(assignments))
        for key, (label, _ast, _line) in assignments.items():
            labels.setdefault(key, label)

    if not output_sets or any(items != output_sets[0] for items in output_sets[1:]):
        # Final-value proof requires the same outputs exactly once in every branch.
        return None
    outputs = output_sets[0]
    if not outputs:
        return None

    # Keep V2 acyclic: guards/RHS values cannot read an output written by this chain.
    written = {item.casefold() for item in outputs}
    for ast in guard_asts:
        for path in _v1._dnf(ast) or []:
            if any(name.casefold() in written for name in path):
                return None
    for assignments in per_branch:
        for _label, rhs_ast, _line in assignments.values():
            for path in _v1._dnf(rhs_ast) or []:
                if any(name.casefold() in written for name in path):
                    return None

    modeled: dict[str, tuple[PLCLogicPath, ...]] = {}
    for output_key in sorted(outputs):
        true_paths: list[dict[str, bool]] = []
        for index, assignments in enumerate(per_branch):
            is_else = index == len(per_branch) - 1
            guard_paths = _exclusive_guard(guard_asts, index, is_else=is_else)
            _label, rhs_ast, _line = assignments[output_key]
            rhs_paths = _v1._dnf(rhs_ast)
            combined = _and_dnf(guard_paths, rhs_paths)
            if combined is None:
                return None
            true_paths.extend(combined)
        deduped = _dedupe_paths(true_paths)
        if deduped is None:
            return None
        modeled[labels[output_key]] = tuple(
            PLCLogicPath(
                tuple(
                    PLCBooleanTerm(tag=name, required=value)
                    for name, value in sorted(path.items(), key=lambda item: item[0].casefold())
                )
            )
            for path in deduped
        )

    assignment_lines = {
        line
        for assignments in per_branch
        for _label, _ast, line in assignments.values()
    }
    control_lines = {line for line, _text in chain["controls"]}
    return {
        "outputs": modeled,
        "assignment_lines": assignment_lines,
        "control_lines": control_lines,
    }


def _iter_st_sources(path: Path):
    _root, files, _total = _v1._preflight_sources(path)
    discovered: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {}

    for source, relative in files:
        try:
            root = ET.parse(source).getroot()
        except ET.ParseError:
            # V1 owns malformed-input errors. This scanner never widens them.
            continue
        for program in (item for item in root.iter() if _v1._local_name(item.tag) == "program"):
            ident = next(
                (item for item in program.iter() if _v1._local_name(item.tag) == "identProgram"),
                None,
            )
            if ident is None:
                continue
            section = (ident.attrib.get("name") or "").strip()
            if not section:
                continue
            st_source = next(
                (item for item in program.iter() if _v1._local_name(item.tag) == "STSource"),
                None,
            )
            if st_source is None:
                continue
            text = "".join(st_source.itertext())
            discovered.append((section, relative, text))
            counts[section.casefold()] = counts.get(section.casefold(), 0) + 1

    # V1 source refs intentionally use section identity, not granular relative
    # filenames. Duplicate section names therefore remain fail-closed in V2.
    for section, relative, text in discovered:
        if counts.get(section.casefold(), 0) == 1:
            yield section, relative, text


def _project_chain_statements(project, section: str, start_line: int, end_line: int):
    result = []
    for statement in project.logic_statements:
        if statement.language != "ST":
            continue
        owner = statement.source.routine or statement.routine or statement.owner_name or ""
        if owner.casefold() != section.casefold():
            continue
        line = _statement_line(statement)
        if line is None or not (start_line <= line <= end_line):
            continue
        result.append(statement)
    return result


def _upgrade_if_chains(project, source_path: Path) -> int:
    replacements: dict[str, object] = {}
    new_output_logic: list[PLCOutputLogic] = []
    modeled_chains = 0

    for section, relative, raw_text in _iter_st_sources(source_path):
        lines = _v1._strip_comments(raw_text).splitlines()
        cursor = 0
        while cursor < len(lines):
            text = lines[cursor].strip()
            if not text:
                cursor += 1
                continue

            if _UNSUPPORTED_OPEN.match(text):
                cursor = _skip_unsupported_region(lines, cursor)
                continue

            if _IF.match(text) is None:
                cursor += 1
                continue

            chain = _collect_if_chain(lines, cursor)
            if chain is None:
                cursor += 1
                continue
            cursor = int(chain["end_index"]) + 1

            modeled = _analyze_chain(chain)
            if modeled is None:
                continue

            start_line = int(chain["start_line"])
            end_line = int(chain["end_line"])
            statements = _project_chain_statements(project, section, start_line, end_line)
            if not statements:
                continue

            assignment_lines = modeled["assignment_lines"]
            control_lines = modeled["control_lines"]
            expected_writers = sum(
                1
                for statement in statements
                if statement.writes and _statement_line(statement) in assignment_lines
            )
            expected_assignment_count = sum(
                len(_split_assignments(text))
                for branch in chain["branches"]
                for _line, text in branch["lines"]
            )
            if expected_writers != expected_assignment_count:
                continue

            valid = True
            for statement in statements:
                line = _statement_line(statement)
                if line in control_lines:
                    if not (_IF.match(statement.text) or _ELSIF.match(statement.text) or _ELSE.match(statement.text)):
                        valid = False
                        break
                    continue
                if line in assignment_lines and statement.writes:
                    continue
                valid = False
                break
            if not valid:
                continue

            origin = f"SCHNEIDER_ST_IF_CHAIN:{start_line}-{end_line}"
            for statement in statements:
                replacements[statement.id] = replace(statement, semantic_state=PLCSemanticState.FULL)

            source = next(
                (statement.source for statement in statements if _IF.match(statement.text)),
                statements[0].source,
            )
            for output, paths in modeled["outputs"].items():
                digest = hashlib.sha1(
                    f"{relative}:{section}:{start_line}:{end_line}:{output}".encode("utf-8")
                ).hexdigest()[:14]
                new_output_logic.append(
                    PLCOutputLogic(
                        id=f"SCHNEIDER-IF2-{digest}",
                        output_tag=output,
                        instruction="ASSIGN_BOOL",
                        paths=paths,
                        source=source,
                        language="ST",
                        origin=origin,
                        semantic_state=PLCSemanticState.FULL,
                    )
                )
            modeled_chains += 1

    if not modeled_chains:
        return 0

    project.logic_statements = [
        replacements.get(statement.id, statement)
        for statement in project.logic_statements
    ]
    existing = {logic.id for logic in project.output_logic}
    project.output_logic.extend(logic for logic in new_output_logic if logic.id not in existing)
    project.instruction_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements
    )
    st = [item for item in project.logic_statements if item.language == "ST"]
    project.st_statement_total = len(st)
    project.st_statement_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in st
    )
    project.partially_modeled_instruction_names = sorted(
        {
            item.language
            for item in project.logic_statements
            if item.semantic_state is not PLCSemanticState.FULL
        }
    )
    return modeled_chains


def _v2_fat_tests(project):
    tests = _v1._fat_tests(project)
    updated = []
    for test in tests:
        limitations = tuple(
            item.replace(
                "bounded Control Expert V1 local Boolean semantics",
                "bounded Control Expert V2 Boolean semantics",
            ).replace(
                "Schneider V1 bounded local Boolean theorem",
                "Schneider V2 bounded Boolean theorem",
            )
            for item in test.limitations
        )
        updated.append(replace(test, limitations=limitations))
    return updated


def schneider_capability_profile_v2(project) -> dict[str, object]:
    base_capability = getattr(_v1, "_ORIGINAL_CAPABILITY", _v1.schneider_capability_profile)
    profile = dict(base_capability(project))
    origins = {logic.origin for logic in project.output_logic if _ORIGIN.match(logic.origin)}
    profile["schema"] = "devagent-schneider-control-expert-capability-v2"
    profile["if_chain_models"] = len(origins)
    profile["if_chain_output_logic"] = sum(
        1 for logic in project.output_logic if _ORIGIN.match(logic.origin)
    )
    profile["bounded_control_flow"] = "top-level complete IF/ELSIF/ELSE Boolean final-value chains"
    profile["control_flow_fail_closed"] = True
    return profile


def analyze_schneider_control_expert_v2(path: Path) -> PLCEngineeringResult:
    original = getattr(_v1, "_ORIGINAL_ANALYZER", _v1.analyze_schneider_control_expert)
    base = original(path)
    project = base.project
    modeled_chains = _upgrade_if_chains(project, path)
    if not modeled_chains:
        return base

    graph = build_dependency_graph(project)
    fat_tests = _v2_fat_tests(project)
    checks = _v1._checks(project, graph, fat_tests)
    profile = schneider_capability_profile_v2(project)

    if profile["static_contract"] == "COMPLETE":
        outcome = PLCOutcome.STATICALLY_VERIFIED
    elif profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    else:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = []
    for item in base.limitations:
        updated = item.replace("Schneider V1", "Schneider V2")
        updated = updated.replace(
            "Bounded top-level ST Boolean assignments and simple series LD contact-to-coil networks are eligible for local static proof. IF/CASE/loop/call/stateful ST and complex LD/FBD/SFC/IL behavior remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
            "Schneider V2 adds a bounded final-value theorem for top-level complete IF/ELSIF/ELSE Boolean assignment chains, while preserving V1 top-level ST Boolean and simple-series LD proof. Nested IF, CASE/loops, calls, stateful behavior, and complex LD/FBD/SFC/IL remain PARTIAL/OPAQUE unless explicitly modeled later.",
        )
        limitations.append(updated)
    limitations.append(
        f"Schneider V2 deterministically modeled {modeled_chains} complete top-level IF/ELSIF/ELSE chain(s). "
        "Missing ELSE/output assignments, duplicate branch writes, nested or enclosing control flow, calls, "
        "self/cyclic output dependencies, duplicate section identities, unsupported expressions, and theorem-size overflow remain fail-closed."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _chain_ranges(project, output: str):
    result = []
    for logic in project.output_logic:
        if logic.output_tag.casefold() != output.casefold():
            continue
        match = _ORIGIN.match(logic.origin)
        if match is None:
            continue
        result.append((logic, int(match.group("start")), int(match.group("end"))))
    return result


def _writers_belong_to_chain(project, output: str, logic):
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
        line = _statement_line(statement)
        same_section = (
            (statement.source.program or statement.owner_name or "").casefold()
            == (logic.source.program or "").casefold()
            and (statement.source.routine or statement.routine or "").casefold()
            == (logic.source.routine or "").casefold()
        )
        if line is None or not same_section or not (start <= line <= end):
            outside.append(statement.id)
    return bool(writers) and not outside, tuple(writers)


def _v2_verify_requirement(previous, requirement, engineering, evidence, tests):
    base = previous(requirement, engineering, evidence, tests)
    if base.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return base

    project = engineering.project
    matched = list(base.matched_tags)
    candidates = []
    for logic in project.output_logic:
        if _ORIGIN.match(logic.origin) is None:
            continue
        explicit = next(
            (tag for tag in matched if tag.casefold() == logic.output_tag.casefold()),
            None,
        )
        if explicit is not None and explicit_bool(requirement.text, explicit) is not None:
            candidates.append(logic)
    if len(candidates) != 1:
        return base

    logic = candidates[0]
    if len(_chain_ranges(project, logic.output_tag)) != 1:
        return base
    safe_writers, writer_ids = _writers_belong_to_chain(project, logic.output_tag, logic)
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

    from devagent.plc import schneider_integration_v1 as _integration

    truth = _integration._bool_truth(logic, assignment, expected)
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
            f"Specified Boolean conditions deterministically imply {logic.output_tag}={'TRUE' if expected else 'FALSE'} in the Schneider V2 complete IF/ELSIF/ELSE final-value theorem; runtime machine behavior still requires FAT when policy requires dynamic proof.",
            combined,
            tuple(matched),
            linked,
        )
    if truth == "CONFLICT":
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            f"Specified Boolean conditions make required {logic.output_tag}={'TRUE' if expected else 'FALSE'} impossible in the Schneider V2 complete IF/ELSIF/ELSE final-value theorem.",
            combined,
            tuple(matched),
        )
    return replace(base, evidence_ids=combined)


def _v2_detect_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    project = engineering.project
    if not str(project.metadata.vendor).casefold().startswith("schneider"):
        return risks

    safe_writer_sets: list[set[str]] = []
    for logic in project.output_logic:
        if _ORIGIN.match(logic.origin) is None:
            continue
        if len(_chain_ranges(project, logic.output_tag)) != 1:
            continue
        safe, writer_ids = _writers_belong_to_chain(project, logic.output_tag, logic)
        if safe:
            safe_writer_sets.append(set(writer_ids))

    filtered = []
    for risk in risks:
        if risk.category == "MULTIPLE_WRITERS" and any(
            set(risk.evidence_ids) == writer_ids for writer_ids in safe_writer_sets
        ):
            continue
        filtered.append(
            replace(
                risk,
                title=risk.title.replace("V1", "V2"),
                summary=risk.summary.replace("Schneider V1", "Schneider V2"),
                consequence=risk.consequence.replace("Schneider V1", "Schneider V2"),
                recommendation=risk.recommendation.replace("Schneider V1", "Schneider V2"),
            )
        )
    return filtered


def _v2_evidence(previous, engineering):
    items = previous(engineering)
    if not str(engineering.project.metadata.vendor).casefold().startswith("schneider"):
        return items
    return [
        replace(
            item,
            summary=item.summary.replace("Schneider Control Expert V1", "Schneider Control Expert V2"),
        )
        for item in items
    ]


def _v2_findings(previous, engineering, valid_evidence_ids):
    items = previous(engineering, valid_evidence_ids)
    if not str(engineering.project.metadata.vendor).casefold().startswith("schneider"):
        return items
    updated = []
    for item in items:
        updated.append(
            replace(
                item,
                title=item.title.replace("V1", "V2"),
                summary=item.summary.replace("Schneider V1", "Schneider V2").replace(
                    "under the V1 contract", "under the V2 contract"
                ),
                recommendation=item.recommendation.replace(
                    "under the V1 contract", "under the V2 contract"
                ),
            )
        )
    return updated


def _v2_render(previous, project) -> str:
    base = previous(project)
    if not str(project.metadata.vendor).casefold().startswith("schneider"):
        return base

    profile = schneider_capability_profile_v2(project)
    base = base.replace("Schneider Control Expert V1", "Schneider Control Expert V2")
    base = base.replace("### Explicit Schneider V1 Boundaries", "### Explicit Schneider V2 Boundaries")
    base = base.replace(
        "- ST IF/CASE/loops/calls, timer/counter/DFB/EFB state, edge behavior, and complex expressions remain PARTIAL until a dedicated theorem models them.",
        "- Complete top-level ST IF/ELSIF/ELSE Boolean chains may receive deterministic final-value proof when every branch assigns the same outputs exactly once and every guard/RHS fits the bounded Boolean grammar.",
    )
    base = base.replace(
        "- Branched/stateful/edge/FFB/control LD and FBD/SFC/IL behavior remain OPAQUE in V1 and require engineer FAT evidence.",
        "- Nested IF, CASE/loops, calls, stateful timer/counter/DFB/EFB behavior, branched/stateful/edge/FFB/control LD, and FBD/SFC/IL behavior remain PARTIAL/OPAQUE in V2 and require engineer FAT evidence.",
    )
    insertion = (
        "### Schneider V2 Bounded Control-Flow Theorem\n\n"
        f"- Modeled complete IF/ELSIF/ELSE chains: **{profile['if_chain_models']}**\n"
        f"- IF-chain Boolean output theorem objects: **{profile['if_chain_output_logic']}**\n"
        "- ELSIF branch selection is modeled exclusively: all prior branch guards must be FALSE before a later ELSIF branch may execute.\n"
        "- Final-value proof requires an ELSE branch and exactly one assignment to the same output set in every branch.\n"
        "- Nested/enclosing control flow, calls, duplicate writes, missing branch assignments, output self-dependencies, duplicate section identity, and theorem-size overflow remain fail-closed.\n"
        "- This remains static exported-source semantics only; DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.\n\n"
    )
    marker = "### Explicit Schneider V2 Boundaries"
    if marker in base and "### Schneider V2 Bounded Control-Flow Theorem" not in base:
        base = base.replace(marker, insertion + marker, 1)
    return base


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    if not hasattr(_v1, "_ORIGINAL_ANALYZER"):
        _v1._ORIGINAL_ANALYZER = _v1.analyze_schneider_control_expert
    if not hasattr(_v1, "_ORIGINAL_CAPABILITY"):
        _v1._ORIGINAL_CAPABILITY = _v1.schneider_capability_profile

    previous_verify = _integration._verify_requirement
    previous_risks = _integration._detect_risks
    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings
    previous_render = _report._render

    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v2
    _v1.schneider_capability_profile = schneider_capability_profile_v2
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v2
    _integration.schneider_capability_profile = schneider_capability_profile_v2

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v2_verify_requirement(previous_verify, requirement, engineering, evidence, tests)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v2_detect_risks(
            previous_risks,
            engineering,
            verifications,
            executions,
            engineering_findings,
        )

    def evidence_index(engineering):
        return _v2_evidence(previous_evidence, engineering)

    def findings(engineering, valid_evidence_ids):
        return _v2_findings(previous_findings, engineering, valid_evidence_ids)

    def render(project):
        return _v2_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._detect_risks = detect_risks
    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _report._render = render
    _INSTALLED = True


__all__ = [
    "analyze_schneider_control_expert_v2",
    "install",
    "schneider_capability_profile_v2",
]
