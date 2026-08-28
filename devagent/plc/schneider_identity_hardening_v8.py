from __future__ import annotations

from dataclasses import replace

from devagent.plc import schneider_identity_types_v8 as _v8
from devagent.plc.models import PLCOutcome, PLCEngineeringResult, PLCSemanticState, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id


_INSTALLED = False
_PREVIOUS_BUILD_SYMBOLS = _v8._build_symbols
_PREVIOUS_ANALYZER = _v8.analyze_schneider_control_expert_v8
_PREVIOUS_CAPABILITY = _v8.schneider_capability_profile_v8
_BOOL_TYPES = {"BOOL", "EBOOL", "BOOLEAN"}


def _hardened_build_symbols(project, types, files):
    """Exclude DDT/DFB interface `<variables>` that V1 inventories as flat tags."""
    raw_globals, _raw_types, _conflicts = _v8._raw_inventory(files)
    original_tags = project.tags
    try:
        project.tags = [tag for tag in original_tags if tag.name.casefold() in raw_globals]
        return _PREVIOUS_BUILD_SYMBOLS(project, types, files)
    finally:
        project.tags = original_tags


def _bool_symbol(raw_ref, facts):
    symbol, resolution = _v8._resolve(str(raw_ref), facts.symbols)
    if symbol is None:
        return False, f"{raw_ref}:{resolution}"
    return symbol.data_type.upper() in _BOOL_TYPES, f"{raw_ref}:{symbol.data_type}"


def _typed_boolean_gaps(project, facts):
    gaps = []
    for logic in project.output_logic:
        if logic.semantic_state is not PLCSemanticState.FULL:
            continue
        okay, detail = _bool_symbol(logic.output_tag, facts)
        reasons = [] if okay else [f"output {detail}"]
        for path in logic.paths:
            for term in path.terms:
                term_okay, term_detail = _bool_symbol(term.tag, facts)
                if not term_okay:
                    reasons.append(f"term {term_detail}")
        if reasons:
            gaps.append((logic.id, logic.output_tag, tuple(dict.fromkeys(reasons))))
    return tuple(gaps)


def _hardened_analyzer(path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    facts = _v8._facts(project)
    if facts is None:
        return base
    gaps = _typed_boolean_gaps(project, facts)
    setattr(project, "_schneider_v8_typed_boolean_gaps", gaps)
    invalid_ids = {item[0] for item in gaps}
    invalid_outputs = {item[1].casefold() for item in gaps}

    if invalid_ids:
        project.output_logic = [
            replace(logic, semantic_state=PLCSemanticState.PARTIAL)
            if logic.id in invalid_ids else logic
            for logic in project.output_logic
        ]
        project.logic_statements = [
            replace(statement, semantic_state=PLCSemanticState.PARTIAL)
            if statement.semantic_state is PLCSemanticState.FULL
            and any(ref.casefold() in invalid_outputs for ref in statement.writes)
            else statement
            for statement in project.logic_statements
        ]
        base.graph.edges = [
            edge for edge in base.graph.edges
            if not (edge.kind == "DEPENDS_ON" and edge.evidence_id in invalid_ids)
        ]

    checks = [item for item in base.static_checks if item.id != "SCHNEIDER_V8_TYPED_BOOLEAN_THEOREM"]
    checks.append(
        StaticCheck(
            "SCHNEIDER_V8_TYPED_BOOLEAN_THEOREM",
            StaticCheckStatus.NOT_PROVEN if gaps else StaticCheckStatus.PASS,
            (
                f"Withheld {len(gaps)} FULL Boolean theorem(s) because canonical V8 type identity is non-Boolean or unresolved."
                if gaps
                else "Every FULL Schneider Boolean output theorem resolves to BOOL/EBOOL/BOOLEAN output and path-term identities."
            ),
            tuple(item[0] for item in gaps),
        )
    )
    outcome = base.outcome
    if gaps and outcome is PLCOutcome.STATICALLY_VERIFIED:
        outcome = PLCOutcome.PARTIALLY_VERIFIED
    limitations = list(base.limitations)
    if gaps:
        limitations.append(
            "Schneider V8 removed Boolean proof from output theorem(s) whose canonical output or dependency types are not BOOL/EBOOL/BOOLEAN; typed DDT/DFB/ARRAY values are never treated as Boolean merely because V1 syntax was parseable."
        )
    return PLCEngineeringResult(outcome, project, base.graph, base.fat_tests, checks, list(dict.fromkeys(limitations)))


def _hardened_capability(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    gaps = getattr(project, "_schneider_v8_typed_boolean_gaps", ())
    profile["typed_boolean_theorem_gaps"] = len(gaps)
    profile["typed_boolean_contract"] = "COMPLETE" if not gaps else "PARTIAL_FAIL_CLOSED"
    if gaps:
        profile["identity_contract"] = "PARTIAL_FAIL_CLOSED"
    return profile


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root
    from devagent.plc import schneider_integration_v1 as _integration

    _v8._build_symbols = _hardened_build_symbols
    _v8.analyze_schneider_control_expert_v8 = _hardened_analyzer
    _v8.schneider_capability_profile_v8 = _hardened_capability
    _root.analyze_schneider_control_expert = _hardened_analyzer
    _root.schneider_capability_profile = _hardened_capability
    _dispatch.analyze_schneider_control_expert = _hardened_analyzer
    _integration.schneider_capability_profile = _hardened_capability

    previous_risks = _integration._detect_risks

    def detect_risks(engineering, verifications, executions, engineering_findings):
        risks = list(previous_risks(engineering, verifications, executions, engineering_findings))
        gaps = getattr(engineering.project, "_schneider_v8_typed_boolean_gaps", ())
        if gaps:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_TYPED_BOOLEAN_V8", *(item[0] for item in gaps)),
                    "TYPE_IDENTITY",
                    "Schneider Boolean theorem conflicts with canonical data type",
                    Severity.HIGH,
                    f"{len(gaps)} previously parseable output theorem(s) use a non-Boolean or unresolved canonical output/dependency type.",
                    "Promoting syntactic Boolean shape across DDT/DFB/ARRAY or unknown typed values could produce a false deterministic PASS.",
                    "Correct the exported type/reference or retain the affected theorem as PARTIAL and execute engineer FAT where behavior is required.",
                    tuple(item[0] for item in gaps),
                )
            )
        return risks

    _integration._detect_risks = detect_risks
    _INSTALLED = True


__all__ = ["install"]
