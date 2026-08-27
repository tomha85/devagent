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
    text = _risk_text(risk)
    if "requirement" in text:
        return "requirement_coverage"
    if "multiple writer" in text or "writer conflict" in text or "ownership" in text:
        return "writer_ownership"
    if any(term in text for term in ("state", "sequence", "transition", "step")):
        return "sequence_state"
    if any(term in text for term in ("partial", "opaque", "unsupported", "semantic coverage")):
        return "semantic_coverage"
    if any(term in text for term in ("unreachable", "call binding", "call graph", "fb", "fc")):
        return "execution_reachability"
    category = re.sub(r"[^a-z0-9]+", "_", risk.category.casefold()).strip("_")
    return category or "other"


def select_top_engineering_risks(risks: list[RiskFinding], *, limit: int = 7) -> list[RiskFinding]:
    """Prioritize severity while keeping the Level 1 view diverse.

    The first pass keeps one representative per engineering family so five
    requirement IDs cannot crowd out writer/sequence/coverage risks. A second
    pass fills remaining slots with other distinct findings.
    """

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
    used_ids: set[str] = set()
    used_families: set[str] = set()

    for risk in ranked:
        family = _risk_family(risk)
        if family in used_families:
            continue
        selected.append(risk)
        used_ids.add(risk.id)
        used_families.add(family)
        if len(selected) >= limit:
            return selected

    for risk in ranked:
        if risk.id in used_ids:
            continue
        selected.append(risk)
        used_ids.add(risk.id)
        if len(selected) >= limit:
            break
    return selected


def _display_title(risk: RiskFinding, all_risks: list[RiskFinding]) -> str:
    family = _risk_family(risk)
    if family == "requirement_coverage":
        count = sum(1 for item in all_risks if _risk_family(item) == family)
        if count > 1:
            return f"Requirement coverage incomplete ({count} related findings)"
    return risk.title


def render_top_engineering_risks(result, *, limit: int = 7) -> str:
    risks = list(result.risks)
    selected = select_top_engineering_risks(risks, limit=limit)
    lines = ["TOP ENGINEERING RISKS"]
    if not selected:
        lines.append("1. No deterministic engineering risk records generated.")
        return "\n".join(lines)

    for index, risk in enumerate(selected, start=1):
        lines.extend(
            [
                f"{index}. {risk.severity.value} — {_display_title(risk, risks)}",
                "",
                f"   Classification: {classify_risk(risk)}",
                "",
                "   Why:",
                f"   {risk.summary}",
                "",
                "   Impact:",
                f"   {risk.consequence}",
                "",
                "   Recommended Action:",
                f"   {risk.recommendation}",
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
