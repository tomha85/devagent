from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc.models import PLCDependencyEdge, PLCSemanticState, StaticCheck, StaticCheckStatus
from devagent.plc import siemens_closeout_v9 as _v9
from devagent.plc import siemens_identity_types_v8 as _v8
from devagent.plc import siemens_tia_v1 as _v1


_INSTALLED = False


def _canonicalize_v8(base, target: Path, files):
    """Apply V8 facts without rewriting the proven V1-V7 schema provenance."""
    project = base.project
    types = _v8._build_types(project, files)
    symbols = _v8._build_symbols(project, types)
    bindings = _v8._bindings(project, symbols)

    # V1 already owns exact same-symbol multiple-writer detection. V8 adds the
    # missing structure/member and array/member ownership overlap only, avoiding
    # duplicate risks for two statements writing the exact same canonical leaf.
    overlaps = tuple(
        pair
        for pair in _v8._writer_overlaps(bindings, symbols)
        if pair[0] != pair[1]
    )
    facts = _v8.SiemensV8Facts(
        types,
        symbols,
        bindings,
        overlaps,
        tuple(item.id for item in bindings if item.resolution.startswith("AMBIGUOUS")),
        tuple(
            item.id
            for item in bindings
            if item.canonical_symbol_id is None
            and not item.resolution.startswith("AMBIGUOUS")
        ),
    )
    setattr(project, "_siemens_v8_identity_facts", facts)

    existing_edges = {
        (edge.source, edge.target, edge.kind, edge.evidence_id)
        for edge in base.graph.edges
    }
    for binding in bindings:
        if not binding.canonical_symbol_id:
            continue
        key = (
            binding.statement_id,
            binding.canonical_symbol_id,
            f"CANONICAL_{binding.access}",
            binding.id,
        )
        if key in existing_edges:
            continue
        base.graph.edges.append(
            PLCDependencyEdge(
                binding.statement_id,
                binding.canonical_symbol_id,
                f"CANONICAL_{binding.access}",
                binding.id,
            )
        )
        existing_edges.add(key)

    checks = list(base.static_checks)
    checks.extend(
        [
            StaticCheck(
                "SIEMENS_V8_CANONICAL_IDENTITY",
                (
                    StaticCheckStatus.PASS
                    if not facts.ambiguous_references and not facts.unresolved_references
                    else StaticCheckStatus.NOT_PROVEN
                ),
                (
                    f"Canonical symbols={len(symbols)}, bindings={len(bindings)}, "
                    f"ambiguous={len(facts.ambiguous_references)}, "
                    f"unresolved={len(facts.unresolved_references)}."
                ),
                tuple((*facts.ambiguous_references, *facts.unresolved_references)),
            ),
            StaticCheck(
                "SIEMENS_V8_WRITER_OVERLAP",
                StaticCheckStatus.NOT_PROVEN if overlaps else StaticCheckStatus.PASS,
                f"Whole-structure/member writer-overlap pairs={len(overlaps)}.",
                tuple(item for pair in overlaps for item in pair),
            ),
        ]
    )

    full_ids = {
        statement.id
        for statement in project.logic_statements
        if statement.semantic_state is PLCSemanticState.FULL
    }
    identity_gap_on_proven_statement = any(
        item.statement_id in full_ids and item.canonical_symbol_id is None
        for item in bindings
    )
    outcome = base.outcome
    if identity_gap_on_proven_statement or overlaps:
        # Preserve the prior outcome if it was already fail-closed; only a
        # previously complete theorem can be demoted by a new V8 identity gap.
        from devagent.plc.models import PLCOutcome

        if outcome is PLCOutcome.STATICALLY_VERIFIED:
            outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = list(base.limitations)
    limitations.extend(
        [
            "Siemens V8 canonical identity/type analysis is fail-closed for unresolved, ambiguous, indirect, dynamically indexed, malformed, or recursively excessive shapes.",
            "ARRAY identity supports ownership/traceability; dynamic index runtime behavior is not promoted to static proof.",
        ]
    )
    return replace(
        base,
        outcome=outcome,
        static_checks=checks,
        limitations=list(dict.fromkeys(limitations)),
    )


def _closeout_v9(base, target: Path):
    """Add exhaustive support accounting without re-judging earlier theorems."""
    project = base.project
    file_count, byte_count, manifest, endings, unicode_present = _v9._source_manifest(target)
    support = _v9._support_regions(project)
    facts = _v9.SiemensV9Facts(
        support,
        _v9._export_variant(project),
        endings,
        unicode_present,
        file_count,
        byte_count,
        manifest,
    )
    setattr(project, "_siemens_v9_closeout_facts", facts)

    checks = list(base.static_checks)
    checks.extend(
        [
            StaticCheck(
                "SIEMENS_V9_SUPPORT_ACCOUNTING",
                StaticCheckStatus.PASS if support.accounting_complete else StaticCheckStatus.NOT_PROVEN,
                (
                    f"Support regions={len(support.regions)}, "
                    f"missing executable statements={len(support.missing_statement_ids)}."
                ),
                (support.id, *support.missing_statement_ids),
            ),
            StaticCheck(
                "SIEMENS_V9_SUPPORT_CONTRACT",
                StaticCheckStatus.PASS if support.contract == "FULL" else StaticCheckStatus.WARN,
                (
                    f"Contract={support.contract}; FULL={support.full}, PARTIAL={support.partial}, "
                    f"OPAQUE={support.opaque}, PROTECTED={support.protected}."
                ),
                tuple(region.id for region in support.regions),
            ),
            StaticCheck(
                "SIEMENS_V9_BUNDLE_MANIFEST",
                StaticCheckStatus.PASS,
                (
                    f"files={file_count}, bytes={byte_count}, line_endings={endings}, "
                    f"unicode={unicode_present}, manifest_sha256={manifest}."
                ),
                (manifest,),
            ),
        ]
    )

    # Important compatibility invariant: the support contract is a coverage
    # disclosure layer, not a second theorem engine. A V2-V7 bounded theorem can
    # remain STATICALLY_VERIFIED while V9 visibly marks raw/control/call regions
    # as PARTIAL/OPAQUE/PROTECTED. Release readiness and V9 risks consume those
    # gaps; they are never silently promoted or hidden.
    limitations = list(base.limitations)
    limitations.append(
        "Siemens V9 commercial closeout is offline engineering-source verification. PARTIAL/OPAQUE/PROTECTED/runtime-dependent regions require explicit engineer evidence; DevAgent does not execute PLCSIM, HIL, a real PLC, or process physics."
    )
    return replace(
        base,
        static_checks=checks,
        limitations=list(dict.fromkeys(limitations)),
    )


def analyze_siemens_tia_v9_hardened(path):
    target = Path(path)
    _root, files = _v8._preflight(target)

    # Run the previously qualified V7 theorem exactly once. V8/V9 are additive
    # identity/coverage layers and therefore preserve its schema_revision field
    # instead of rewriting historical provenance to V8/V9.
    base = _v8._PREVIOUS_ANALYZER(target)
    v8 = _canonicalize_v8(base, target, files)
    return _closeout_v9(v8, target)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration

    _v1.analyze_siemens_tia = analyze_siemens_tia_v9_hardened
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v9_hardened
    _integration.siemens_capability_profile = _v9.siemens_capability_profile_v9
    _INSTALLED = True


__all__ = ["analyze_siemens_tia_v9_hardened", "install"]
