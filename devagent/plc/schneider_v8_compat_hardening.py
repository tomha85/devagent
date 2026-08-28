from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc import schneider_identity_hardening_v8 as _hard
from devagent.plc import schneider_identity_types_v8 as _v8
from devagent.plc.models import PLCDependencyEdge, PLCEngineeringResult, PLCOutcome, PLCSemanticState, StaticCheck, StaticCheckStatus


_INSTALLED = False
_BASE_ANALYZER = _v8._PREVIOUS_ANALYZER


def _canonicalize_v8(path) -> PLCEngineeringResult:
    """Add V8 identity facts without rewriting V1-V7 theorem provenance."""
    target = Path(path)
    _root, files, _total = _v8._v1._preflight_sources(target)
    base = _BASE_ANALYZER(target)
    project = base.project

    types = _v8._build_types(project, files)
    symbols, identity_conflicts = _v8._build_symbols(project, types, files)
    bindings = _v8._bindings(project, symbols)
    io_points = _v8._io_points(symbols)
    dfb_instances = _v8._dfb_identities(project, symbols, types)
    whole_member = tuple(pair for pair in _v8._writer_overlaps(bindings, symbols) if pair[0] != pair[1])
    physical_aliases = _v8._physical_aliases(symbols, bindings)
    ambiguous = tuple(item.id for item in bindings if item.resolution.startswith("AMBIGUOUS"))
    unresolved = tuple(
        item.id
        for item in bindings
        if item.canonical_symbol_id is None and not item.resolution.startswith("AMBIGUOUS")
    )
    address_conflicts = tuple(item for item in identity_conflicts if item.startswith("ADDRESS:"))
    facts = _v8.SchneiderV8Facts(
        _v8._project_identity(project),
        types,
        symbols,
        bindings,
        io_points,
        dfb_instances,
        whole_member,
        physical_aliases,
        ambiguous,
        unresolved,
        identity_conflicts,
        address_conflicts,
    )
    setattr(project, "_schneider_v8_identity_facts", facts)

    existing_edges = {
        (edge.source, edge.target, edge.kind, edge.evidence_id)
        for edge in base.graph.edges
    }
    for binding in bindings:
        if not binding.canonical_symbol_id:
            continue
        key = (binding.statement_id, binding.canonical_symbol_id, f"CANONICAL_{binding.access}", binding.id)
        if key not in existing_edges:
            base.graph.edges.append(PLCDependencyEdge(*key))
            existing_edges.add(key)
    for point in io_points:
        key = (point.symbol_id, point.id, "LOCATED_AT", point.id)
        if key not in existing_edges:
            base.graph.edges.append(PLCDependencyEdge(*key))
            existing_edges.add(key)

    full_ids = {
        statement.id
        for statement in project.logic_statements
        if statement.semantic_state is PLCSemanticState.FULL
    }
    identity_gap = any(
        item.statement_id in full_ids and item.canonical_symbol_id is None
        for item in bindings
    )
    outcome = base.outcome
    if outcome is PLCOutcome.STATICALLY_VERIFIED and (
        identity_gap or identity_conflicts or whole_member or physical_aliases
    ):
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    checks = [item for item in base.static_checks if not item.id.startswith("SCHNEIDER_V8_")]
    checks.extend(
        [
            StaticCheck(
                "SCHNEIDER_V8_CANONICAL_IDENTITY",
                StaticCheckStatus.PASS if not ambiguous and not unresolved and not identity_conflicts else StaticCheckStatus.NOT_PROVEN,
                f"Canonical symbols={len(symbols)}, bindings={len(bindings)}, ambiguous={len(ambiguous)}, unresolved={len(unresolved)}, identity conflicts={len(identity_conflicts)}.",
                tuple((*ambiguous, *unresolved, *identity_conflicts)),
            ),
            StaticCheck(
                "SCHNEIDER_V8_DDT_DFB_TYPE_IDENTITY",
                StaticCheckStatus.PASS if types else StaticCheckStatus.WARN,
                f"Canonical types={len(types)}: DDT={sum(item.kind == 'DDT' for item in types)}, DFB={sum(item.kind == 'DFB' for item in types)}, ARRAY={sum(item.kind == 'ARRAY' for item in types)}, ENUM={sum(item.kind == 'ENUM' for item in types)}.",
                tuple(item.id for item in types),
            ),
            StaticCheck(
                "SCHNEIDER_V8_IO_ADDRESS_IDENTITY",
                StaticCheckStatus.NOT_PROVEN if address_conflicts or physical_aliases else StaticCheckStatus.PASS,
                f"Located/topological identities={len(io_points)}, source metadata conflicts={len(address_conflicts)}, physical alias pairs={len(physical_aliases)}.",
                tuple([*(item.id for item in io_points), *address_conflicts, *(value for pair in physical_aliases for value in pair)]),
            ),
            StaticCheck(
                "SCHNEIDER_V8_WRITER_OWNERSHIP",
                StaticCheckStatus.NOT_PROVEN if whole_member else StaticCheckStatus.PASS,
                f"Whole-structure/member canonical writer overlap pairs={len(whole_member)}.",
                tuple(value for pair in whole_member for value in pair),
            ),
        ]
    )

    limitations = list(base.limitations)
    limitations.extend(
        [
            "Schneider V8 canonical identity/type/I/O analysis is fail-closed for unresolved or conflicting symbols, whole/member ownership overlap, duplicate referenced physical located addresses, malformed XML metadata, and recursively excessive type expansion.",
            "ARRAY wildcard identity proves ownership/traceability only; dynamic index values, physical module behavior, I/O update timing, Control Expert Simulator, HIL, SIL/PL, and real Modicon execution are not statically proven.",
            "Topological/located address identity records exported Control Expert engineering metadata; it does not certify wiring, channel health, force state, field device behavior, or that the downloaded controller image matches the export.",
        ]
    )
    identity_result = PLCEngineeringResult(
        outcome,
        project,
        base.graph,
        base.fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )
    return _apply_typed_boolean_hardening(identity_result)


def _apply_typed_boolean_hardening(base: PLCEngineeringResult) -> PLCEngineeringResult:
    project = base.project
    facts = _v8._facts(project)
    if facts is None:
        return base
    gaps = _hard._typed_boolean_gaps(project, facts)
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
            edge
            for edge in base.graph.edges
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
    return PLCEngineeringResult(
        outcome,
        project,
        base.graph,
        base.fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root

    _v8.analyze_schneider_control_expert_v8 = _canonicalize_v8
    _hard._hardened_analyzer = _canonicalize_v8
    _root.analyze_schneider_control_expert = _canonicalize_v8
    _dispatch.analyze_schneider_control_expert = _canonicalize_v8
    _INSTALLED = True


__all__ = ["install"]
