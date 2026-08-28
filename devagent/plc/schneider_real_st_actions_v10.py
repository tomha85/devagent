from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import re
from pathlib import Path

from devagent.plc import schneider_closeout_v9 as _v9
from devagent.plc import schneider_control_expert_v1 as _v1
from devagent.plc.models import (
    FATTestCase,
    PLCDependencyEdge,
    PLCSemanticState,
    PLCSourceRef,
    StaticCheck,
    StaticCheckStatus,
)


_INSTALLED = False
_PREVIOUS_ANALYZER = _v9.analyze_schneider_control_expert_v9
_PREVIOUS_CAPABILITY = _v9.schneider_capability_profile_v9
_ACTION_SCHEMA = "devagent-schneider-real-st-actions-v1"

_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")
_SIMPLE_REF = re.compile(
    r"^(?:%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_ARITH_TOKEN = re.compile(
    r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)|"
    r"(?P<ref>%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)|"
    r"(?P<op>\+|-|\*|/|\(|\)|\bMOD\b))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SchneiderSTLocalAction:
    id: str
    statement_id: str
    family: str
    output_tag: str
    input_refs: tuple[str, ...]
    expression: str
    expected_effect: str
    source: PLCSourceRef
    execution_condition_proven: bool


@dataclass(frozen=True)
class SchneiderSTActionFacts:
    actions: tuple[SchneiderSTLocalAction, ...]
    partial_st_statements: int
    modeled_partial_st_statements: int
    withheld_partial_st_statements: int


def _facts(project) -> SchneiderSTActionFacts | None:
    return getattr(project, "_schneider_real_st_action_facts", None)


def _tokenize_arithmetic(expr: str):
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expr):
        if expr[position:].strip() == "":
            break
        match = _ARITH_TOKEN.match(expr, position)
        if match is None:
            return None
        if match.group("number") is not None:
            tokens.append(("NUMBER", match.group("number")))
        elif match.group("ref") is not None:
            value = match.group("ref")
            if value.upper() == "MOD":
                tokens.append(("OP", "MOD"))
            else:
                tokens.append(("REF", value))
        else:
            tokens.append(("OP", match.group("op").upper()))
        position = match.end()
    return tokens or None


def _arithmetic_refs(expr: str) -> tuple[str, ...] | None:
    """Validate a deliberately small pure arithmetic expression grammar.

    Supported operands are fixed references and numeric literals. Operators are
    +, -, *, /, and MOD with normal parenthesis/precedence and unary +/-.
    Function calls, indexed access, time literals, comparisons, assignments, and
    unknown tokens are rejected. This proves only the local assignment effect
    when the source statement executes; it does not prove the enclosing control
    path executes.
    """

    tokens = _tokenize_arithmetic(expr)
    if not tokens:
        return None
    index = 0
    refs: list[str] = []

    def primary() -> bool:
        nonlocal index
        if index >= len(tokens):
            return False
        kind, value = tokens[index]
        if kind == "OP" and value in {"+", "-"}:
            index += 1
            return primary()
        if kind in {"NUMBER", "REF"}:
            if kind == "REF" and value not in refs:
                refs.append(value)
            index += 1
            return True
        if kind == "OP" and value == "(":
            index += 1
            if not expression():
                return False
            if index >= len(tokens) or tokens[index] != ("OP", ")"):
                return False
            index += 1
            return True
        return False

    def term() -> bool:
        nonlocal index
        if not primary():
            return False
        while index < len(tokens) and tokens[index][0] == "OP" and tokens[index][1] in {"*", "/", "MOD"}:
            index += 1
            if not primary():
                return False
        return True

    def expression() -> bool:
        nonlocal index
        if not term():
            return False
        while index < len(tokens) and tokens[index][0] == "OP" and tokens[index][1] in {"+", "-"}:
            index += 1
            if not term():
                return False
        return True

    if not expression() or index != len(tokens):
        return None
    return tuple(refs)


def _assignment_action(statement) -> SchneiderSTLocalAction | None:
    if statement.language.upper() != "ST" or statement.semantic_state is not PLCSemanticState.PARTIAL:
        return None
    match = _v1._ASSIGNMENT.match(statement.text)
    if match is None:
        return None
    lhs = _v1._lhs_ref(match.group("lhs"))
    if lhs is None:
        return None
    rhs = match.group("rhs").strip().rstrip(";").strip()
    if not rhs or "," in rhs or "[" in rhs or "]" in rhs:
        return None

    family: str
    refs: tuple[str, ...]
    expected: str

    if _NUMBER.fullmatch(rhs):
        family = "CONSTANT_ASSIGNMENT"
        refs = ()
        expected = f"When this ST statement executes, {lhs} receives numeric literal {rhs}."
    elif _SIMPLE_REF.fullmatch(rhs) and rhs.upper() not in {"TRUE", "FALSE"}:
        # A single fixed reference is a data move regardless of whether the
        # canonical type is BOOL, INT, REAL, or another scalar type.
        family = "DATA_MOVE"
        refs = (rhs,)
        expected = f"When this ST statement executes, {lhs} receives the value of {rhs}."
    else:
        # Reuse the exact V1 Boolean parser so local-action semantics can never
        # silently accept a wider Boolean grammar than the qualified theorem.
        bool_ast = _v1._parse_bool_ast(rhs)
        if bool_ast is not None and _v1._dnf(bool_ast) is not None:
            family = "BOOLEAN_ASSIGNMENT"
            refs = tuple(_v1._extract_refs(rhs))
            expected = f"When this ST statement executes, {lhs} receives Boolean expression {rhs}."
        else:
            arithmetic_refs = _arithmetic_refs(rhs)
            if arithmetic_refs is None:
                return None
            family = "ARITHMETIC_ASSIGNMENT"
            refs = arithmetic_refs
            expected = f"When this ST statement executes, {lhs} receives arithmetic expression {rhs}."

    digest = hashlib.sha1(f"{statement.id}:{family}:{lhs}:{rhs}".encode("utf-8")).hexdigest()[:14]
    return SchneiderSTLocalAction(
        id=f"SCHNEIDER-ST-ACTION10-{digest}",
        statement_id=statement.id,
        family=family,
        output_tag=lhs,
        input_refs=refs,
        expression=rhs,
        expected_effect=expected,
        source=statement.source,
        # All models in this layer originate from already-PARTIAL statements.
        # The local assignment effect is understood, but enclosing execution
        # conditions remain governed by the existing V1-V9 fail-closed stack.
        execution_condition_proven=False,
    )


def _collect_actions(project) -> SchneiderSTActionFacts:
    partial = [
        item
        for item in project.logic_statements
        if item.language.upper() == "ST" and item.semantic_state is PLCSemanticState.PARTIAL
    ]
    actions = tuple(
        action
        for statement in partial
        for action in [_assignment_action(statement)]
        if action is not None
    )
    modeled_ids = {item.statement_id for item in actions}
    return SchneiderSTActionFacts(
        actions=actions,
        partial_st_statements=len(partial),
        modeled_partial_st_statements=len(modeled_ids),
        withheld_partial_st_statements=len(partial) - len(modeled_ids),
    )


def _augment_graph(graph, facts: SchneiderSTActionFacts) -> None:
    seen = {(edge.source, edge.target, edge.kind, edge.evidence_id) for edge in graph.edges}
    for action in facts.actions:
        for dependency in action.input_refs:
            key = (action.output_tag, dependency, "ST_LOCAL_ACTION_DEPENDS_ON", action.id)
            if key in seen:
                continue
            seen.add(key)
            graph.edges.append(
                PLCDependencyEdge(
                    source=action.output_tag,
                    target=dependency,
                    kind="ST_LOCAL_ACTION_DEPENDS_ON",
                    evidence_id=action.id,
                )
            )


def _action_fat_tests(facts: SchneiderSTActionFacts) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for action in facts.actions:
        digest = hashlib.sha1(f"{action.id}:runtime".encode("utf-8")).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SCHNEIDER-ST-ACTION-{digest}",
                title=f"Verify ST local {action.family.lower().replace('_', ' ')} for {action.output_tag} at {action.source.locator}",
                source=action.source,
                output_tag=action.output_tag,
                preconditions={},
                expected=action.expected_effect,
                method="RUNTIME_FAT_REQUIRED",
                scenario="SCHNEIDER_ST_LOCAL_ACTION",
                limitations=(
                    "The local assignment expression is deterministically recognized, but the enclosing ST execution condition is not proven by this layer.",
                    "PASS requires engineer-executed runtime evidence; later writers, task ordering, stateful blocks, I/O refresh, and process physics remain separate evidence boundaries.",
                ),
                purpose=(
                    "Confirm the real Control Expert ST assignment effect without converting an unmodeled control path into a static PASS."
                ),
                action_steps=(
                    "Drive the engineer-selected conditions that execute the referenced ST statement.",
                    f"Observe {action.output_tag} and all referenced inputs while the statement executes.",
                    "Capture timestamped before/after values and retain them with the exact PLC project hash.",
                ),
                watch_tags=tuple(dict.fromkeys((action.output_tag, *action.input_refs))),
                evidence_required=(
                    "Timestamped observed input/output values",
                    "Exact analyzed PLC project SHA-256",
                    "Engineer/test-environment identification",
                ),
                why_required=(
                    "The assignment effect is locally modeled but the current V1-V9 theorem intentionally withholds the enclosing execution condition."
                ),
                failure_implication=(
                    "Observed behavior differs from the imported Control Expert ST assignment or the tested runtime path is not equivalent to the analyzed project."
                ),
            )
        )
    return tests


def schneider_capability_profile_v10(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    # Preserve the V9 commercial support-contract schema. Real-ST local actions
    # are an additive sub-capability and must not rewrite existing evidence/API
    # consumers that key on the V9 capability schema.
    profile["real_st_action_schema"] = _ACTION_SCHEMA
    if facts is None:
        profile.update(
            {
                "real_st_local_actions": 0,
                "real_st_local_action_families": {},
                "partial_st_with_local_action_semantics": 0,
                "partial_st_withheld_from_local_action_semantics": 0,
                "real_st_local_actions_promote_v9_support": False,
            }
        )
        return profile
    families = Counter(item.family for item in facts.actions)
    profile.update(
        {
            "real_st_local_actions": len(facts.actions),
            "real_st_local_action_families": dict(sorted(families.items())),
            "partial_st_with_local_action_semantics": facts.modeled_partial_st_statements,
            "partial_st_withheld_from_local_action_semantics": facts.withheld_partial_st_statements,
            "real_st_local_actions_promote_v9_support": False,
            "real_st_local_action_contract": (
                "Pure fixed-destination ST assignments receive deterministic local effect/dependency models and engineer FAT procedures. "
                "Already-PARTIAL source statements stay PARTIAL until their enclosing execution condition, writer ownership, and type/scope boundaries are independently proven."
            ),
        }
    )
    return profile


def analyze_schneider_control_expert_v10(path: str | Path):
    base = _PREVIOUS_ANALYZER(Path(path))
    project = base.project
    facts = _collect_actions(project)
    setattr(project, "_schneider_real_st_action_facts", facts)

    _augment_graph(base.graph, facts)
    generated = _action_fat_tests(facts)
    existing_tests = {item.id for item in base.fat_tests}
    fat_tests = [*base.fat_tests, *(item for item in generated if item.id not in existing_tests)]

    checks = [item for item in base.static_checks if item.id != "SCHNEIDER_V10_REAL_ST_LOCAL_ACTIONS"]
    families = Counter(item.family for item in facts.actions)
    checks.append(
        StaticCheck(
            id="SCHNEIDER_V10_REAL_ST_LOCAL_ACTIONS",
            status=StaticCheckStatus.PASS if facts.actions else StaticCheckStatus.WARN,
            summary=(
                f"Modeled {len(facts.actions)} deterministic local ST assignment effect(s) across "
                f"{facts.modeled_partial_st_statements} already-PARTIAL statement(s); "
                f"{facts.withheld_partial_st_statements} PARTIAL ST statement(s) remain outside this local action grammar. "
                f"Families={dict(sorted(families.items()))}. No V9 PARTIAL statement was promoted to FULL."
            ),
            evidence=tuple(item.id for item in facts.actions),
        )
    )

    limitations = list(base.limitations)
    limitations.append(
        "Schneider V10 real-ST local action semantics model only pure fixed-destination Boolean, constant, data-move, and bounded arithmetic assignments. "
        "They do not prove that enclosing IF/CASE/loop/call paths execute and therefore do not promote existing V9 PARTIAL regions to FULL."
    )
    return replace(
        base,
        fat_tests=fat_tests,
        static_checks=checks,
        limitations=list(dict.fromkeys(limitations)),
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root
    from devagent.plc import schneider_integration_v1 as _integration

    _v9.analyze_schneider_control_expert_v9 = analyze_schneider_control_expert_v10
    _v9.schneider_capability_profile_v9 = schneider_capability_profile_v10
    _root.schneider_capability_profile = schneider_capability_profile_v10
    _integration.schneider_capability_profile = schneider_capability_profile_v10
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v10
    _INSTALLED = True


__all__ = [
    "SchneiderSTActionFacts",
    "SchneiderSTLocalAction",
    "analyze_schneider_control_expert_v10",
    "schneider_capability_profile_v10",
    "install",
]
