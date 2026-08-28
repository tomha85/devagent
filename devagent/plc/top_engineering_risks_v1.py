from __future__ import annotations

import re

from devagent.plc.production_models import RiskFinding


_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
}

_FIX_TERMS = (
    "multiple writer",
    "writer conflict",
    "conflict",
    "contradict",
    "ambiguous",
    "dangling",
    "duplicate",
    "recursion",
    "cycle",
    "ownership",
)

_RUNTIME_TERMS = (
    "runtime",
    "fat",
    "simulator",
    "hil",
    "timing",
    "timer",
    "counter",
    "retained",
    "scan order",
    "motion",
    "not deterministically proven",
    "not proven",
    "partial",
    "opaque",
    "execution evidence",
)

_CALL_BINDING_TERMS = (
    "ambiguous_or_unresolved_target",
    "unresolved target",
    "unresolved call",
    "call binding",
    "call graph",
    "withheld call proof",
    "instance binding",
    "recursive path",
)


def _risk_text(risk: RiskFinding) -> str:
    return " ".join(
        (
            risk.category,
            risk.title,
            risk.summary,
            risk.consequence,
            risk.recommendation,
        )
    ).casefold()


def classify_risk(risk: RiskFinding) -> str:
    """Conservatively classify an existing deterministic risk for engineer action.

    Presentation only: this never changes risk severity, proof, FAT status, or
    release readiness. Unknown cases stay REVIEW_REQUIRED rather than telling an
    engineer to modify PLC code without deterministic support.
    """

    text = _risk_text(risk)
    fix = any(term in text for term in _FIX_TERMS)
    runtime = any(term in text for term in _RUNTIME_TERMS)
    if fix and runtime:
        return "FIX_AND_FAT"
    if fix:
        return "FIX_RECOMMENDED"
    if runtime:
        return "FAT_REQUIRED"
    return "REVIEW_REQUIRED"


def _risk_family(risk: RiskFinding) -> str:
    """Return the engineering root-cause family used only for Level 1 grouping.

    Root-cause signals intentionally take precedence over downstream prose. An
    explicit requirement-category risk is a requirement risk, but a call/writer/
    semantic risk must not become ``requirement_coverage`` merely because its
    consequence mentions downstream requirement traceability.
    """

    text = _risk_text(risk)
    category = risk.category.casefold()
    risk_id = risk.id.casefold()
    if category == "requirement" or category.startswith("requirement_") or risk_id.startswith("risk-req"):
        return "requirement_coverage"
    if "test_failure" in category or "fat test" in text and "failed" in text:
        return "test_failure"
    if any(term in text for term in _CALL_BINDING_TERMS):
        return "call_binding"
    if "multiple writer" in text or "writer conflict" in text or "ownership" in text:
        return "writer_ownership"
    if any(term in text for term in ("partial", "opaque", "unsupported", "semantic coverage")):
        return "semantic_coverage"
    if any(term in text for term in ("state", "sequence", "transition", "step")):
        return "sequence_state"
    if any(term in text for term in ("unreachable", "reachability", "dead logic")):
        return "execution_reachability"
    category = re.sub(r"[^a-z0-9]+", "_", category).strip("_")
    return category or "other"


def _family_members(risk: RiskFinding, all_risks: list[RiskFinding]) -> list[RiskFinding]:
    family = _risk_family(risk)
    return [item for item in all_risks if _risk_family(item) == family]


def _aggregate_classification(members: list[RiskFinding]) -> str:
    classes = {classify_risk(item) for item in members}
    if "FIX_AND_FAT" in classes or {"FIX_RECOMMENDED", "FAT_REQUIRED"} <= classes:
        return "FIX_AND_FAT"
    if "FIX_RECOMMENDED" in classes:
        return "FIX_RECOMMENDED"
    if "FAT_REQUIRED" in classes:
        return "FAT_REQUIRED"
    return "REVIEW_REQUIRED"


def select_top_engineering_risks(risks: list[RiskFinding], *, limit: int = 7) -> list[RiskFinding]:
    """Prioritize severity and show each engineering root-cause family once."""

    ranked = sorted(
        risks,
        key=lambda risk: (
            _SEVERITY_ORDER.get(risk.severity.value, 99),
            risk.category.casefold(),
            risk.title.casefold(),
            risk.id.casefold(),
        ),
    )
    selected: list[RiskFinding] = []
    used_families: set[str] = set()

    for risk in ranked:
        family = _risk_family(risk)
        if family in used_families:
            continue
        selected.append(risk)
        used_families.add(family)
        if len(selected) >= limit:
            break
    return selected


def _display_title(risk: RiskFinding, all_risks: list[RiskFinding]) -> str:
    family = _risk_family(risk)
    members = _family_members(risk, all_risks)
    count = len(members)
    text = " ".join(_risk_text(item) for item in members)

    if family == "call_binding":
        if "schneider" in text or "control expert" in text or "unity pro" in text:
            vendor = "Schneider"
        elif "siemens" in text or "tia" in text:
            vendor = "Siemens"
        elif "rockwell" in text or "studio 5000" in text or "logix" in text:
            vendor = "Rockwell"
        else:
            vendor = "PLC"
        suffix = f" ({count} related findings)" if count > 1 else ""
        return f"Unresolved/ambiguous {vendor} call bindings{suffix}"
    if family == "requirement_coverage":
        suffix = f" ({count} related findings)" if count > 1 else ""
        return f"Requirement coverage incomplete{suffix}"
    if family == "writer_ownership" and count > 1:
        return f"Multiple-writer / ownership risks ({count} related findings)"
    if family == "sequence_state" and count > 1:
        return f"Sequence/state behavior requires review ({count} related findings)"
    if family == "execution_reachability" and count > 1:
        return f"Execution/reachability gaps ({count} related findings)"
    if family == "test_failure" and count > 1:
        return f"FAT execution failures ({count} related findings)"
    return risk.title


def _aggregate_details(
    risk: RiskFinding,
    all_risks: list[RiskFinding],
) -> tuple[str, str, str, str]:
    members = _family_members(risk, all_risks)
    family = _risk_family(risk)
    count = len(members)
    classification = _aggregate_classification(members)

    if family == "call_binding":
        why = (
            f"{count} PLC call finding(s) cannot be deterministically bound to an exact target, interface, or instance in the imported engineering evidence. "
            f"Representative evidence: {risk.summary}"
        )
        impact = (
            "Downstream block behavior, call reachability, and requirement/FAT traceability may depend on unresolved execution context, so these paths cannot be promoted to static verification."
        )
        recommendation = (
            "Resolve or export the exact call/interface/instance evidence where possible. If the binding remains runtime-dependent, execute the linked engineer FAT procedures in the approved simulator/HIL/controller environment before release."
        )
        return classification, why, impact, recommendation

    if family == "requirement_coverage" and count > 1:
        why = f"{count} requirement verification finding(s) still lack sufficient deterministic or authenticated runtime evidence."
        impact = "The release evidence package cannot claim those customer requirements are verified."
        recommendation = "Map each affected requirement to concrete source evidence and the appropriate FAT scenario, then import authenticated execution results where runtime proof is required."
        return classification, why, impact, recommendation

    if family == "writer_ownership" and count > 1:
        why = f"{count} writer/ownership finding(s) indicate PLC values are assigned from more than one normalized source location or execution path."
        impact = "Final values may depend on scan order, block/task order, branch conditions, or later assignments."
        recommendation = "Review authoritative ownership and execution order; consolidate writers where appropriate or document intentional arbitration, then rerun affected FAT procedures."
        return classification, why, impact, recommendation

    if family == "sequence_state" and count > 1:
        why = f"{count} sequence/state finding(s) depend on execution paths, transitions, or runtime state that are not completely proven statically."
        impact = "The machine sequence may stall, skip, repeat, or enter an unintended state under conditions outside the bounded theorem."
        recommendation = "Review the affected transition logic and execute the generated sequence FAT scenarios in an approved runtime environment."
        return classification, why, impact, recommendation

    return classification, risk.summary, risk.consequence, risk.recommendation


def render_top_engineering_risks(result, *, limit: int = 7) -> str:
    risks = list(result.risks)
    selected = select_top_engineering_risks(risks, limit=limit)
    lines = ["TOP ENGINEERING RISKS"]
    if not selected:
        lines.append("1. No deterministic engineering risk records generated.")
        return "\n".join(lines)

    for index, risk in enumerate(selected, start=1):
        classification, why, impact, recommendation = _aggregate_details(risk, risks)
        lines.extend(
            [
                f"{index}. {risk.severity.value} — {_display_title(risk, risks)}",
                "",
                f"   Classification: {classification}",
                "",
                "   Why:",
                f"   {why}",
                "",
                "   Impact:",
                f"   {impact}",
                "",
                "   Recommended Action:",
                f"   {recommendation}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def install() -> None:
    """Replace only the Level 1 risk presentation; preserve all detailed artifacts."""

    from devagent.plc import report_levels

    if getattr(report_levels, "_TOP_ENGINEERING_RISKS_V1_INSTALLED", False):
        return

    original = report_levels.render_console_summary

    def render_console_summary(result, *, output_dir=None, scenario_limit=20):
        base = original(result, output_dir=output_dir, scenario_limit=scenario_limit)
        start_marker = "TOP FINDINGS\n"
        end_marker = "\nTOP RECOMMENDATIONS"
        start = base.find(start_marker)
        if start < 0:
            return base
        end = base.find(end_marker, start)
        if end < 0:
            return base
        replacement = render_top_engineering_risks(result)
        return base[:start] + replacement + base[end:]

    report_levels.render_console_summary = render_console_summary
    report_levels._TOP_ENGINEERING_RISKS_V1_INSTALLED = True


__all__ = [
    "classify_risk",
    "install",
    "render_top_engineering_risks",
    "select_top_engineering_risks",
]
