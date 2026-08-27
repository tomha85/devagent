from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc import siemens_flgnet_v4 as _v4


_INSTALLED = False
_PREVIOUS_ANALYZER = _v4.analyze_siemens_tia_v4


def _warning_matches(statement, warning: str) -> bool:
    """Match the exact V1 warning for the compile-unit statement.

    PLCSourceRef.locator is a human-readable full controller/program/routine path;
    the V1 warning uses the canonical PLCLogicStatement locator (``Network N``).
    V4 must clear only the warning for a network it actually upgraded to FULL.
    """

    prefix = (
        f"TIA XML {statement.language} network "
        f"{statement.source.program or statement.owner_name}/"
        f"{statement.locator} "
    )
    return (
        warning.startswith(prefix)
        and "V1 withholds executable behavior proof" in warning
    )


def _withheld_traceability_warning(statement, reason: str) -> str:
    """Keep the V1 traceability contract while reporting the V4 reason.

    Older consumers/tests rely on the explicit statement that an unsupported
    Openness network was structurally imported. V4 may refine why proof is
    withheld, but it must not erase that useful provenance signal.
    """

    return (
        f"TIA XML {statement.language} network "
        f"{statement.source.program or statement.owner_name}/"
        f"{statement.locator} is structurally imported but V4 withholds "
        f"executable behavior proof ({reason}); engineer FAT is required."
    )


def _hardened_analyzer(path: Path):
    result = _PREVIOUS_ANALYZER(path)
    facts = getattr(result.project, "_siemens_v4_facts", None)
    if facts is None:
        return result

    by_id = {item.id: item for item in result.project.logic_statements}
    modeled = {item.statement_id for item in facts.modeled}

    # FULL V4 networks legitimately close their old V1 OPAQUE warning. Withheld
    # networks keep a structurally-imported provenance warning, augmented with
    # the exact V4 fail-closed reason. This preserves backward compatibility
    # without weakening the theorem boundary.
    warnings = list(result.project.warnings)
    for fact in facts.withheld:
        statement = by_id.get(fact.statement_id)
        if statement is None:
            continue
        warning = _withheld_traceability_warning(statement, fact.reason)
        if warning not in warnings:
            warnings.append(warning)
    result.project.warnings = list(dict.fromkeys(warnings))

    limitations = []
    for item in result.limitations:
        if any(
            statement_id in modeled
            and statement_id in by_id
            and _warning_matches(by_id[statement_id], item)
            for statement_id in modeled
        ):
            continue
        item = item.replace(
            "Only bounded top-level SCL assignment/Boolean dataflow is eligible for static proof in V1. IF/CASE/loop/call semantics and LAD/FBD/GRAPH/STL XML networks remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
            "Siemens V4 additionally proves only the bounded LAD/FBD FlgNet Boolean subset declared in the V4 capability profile; every unsupported visual network remains PARTIAL/OPAQUE and requires engineer FAT.",
        )
        item = item.replace(
            "Siemens V2 adds a bounded theorem for single-level complete IF/ELSIF/ELSE Boolean assignment chains. Nested control flow, CASE/loops, calls, complex expressions, and LAD/FBD/GRAPH/STL XML networks remain PARTIAL/OPAQUE unless explicitly modeled by a later theorem.",
            "Siemens V4 retains the V2 bounded IF/ELSIF/ELSE theorem and adds only its declared LAD/FBD FlgNet Boolean subset; other controls, calls outside V3 closure, visual instructions, GRAPH/STL, and unsupported networks remain fail-closed.",
        )
        limitations.append(item)
    return replace(result, limitations=list(dict.fromkeys(limitations)))


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_tia_v1 as _v1

    # Patch the warning matcher before the analyzer executes so a FULL V4 network
    # can legitimately close the V1 XML-withheld warning without clearing any
    # warning for a different network.
    _v4._legacy_warning_matches = _warning_matches
    _v4.analyze_siemens_tia_v4 = _hardened_analyzer
    _v1.analyze_siemens_tia = _hardened_analyzer
    _dispatch.analyze_siemens_tia = _hardened_analyzer
    _INSTALLED = True


__all__ = ["install"]
