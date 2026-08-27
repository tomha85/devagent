from __future__ import annotations

from collections import Counter

from devagent.plc.production_models import ExecutionStatus, RequirementStatus, Severity


_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape(value) for value in row) + " |")
    lines.append("")
    return lines


def _release_recommendation(result) -> str:
    readiness = result.readiness
    if readiness is None:
        return "NOT EVALUATED — engineering review output is available, but no release-readiness decision was evaluated."
    status = readiness.status.value
    if status == "APPROVED_FOR_RELEASE":
        return "APPROVED BY POLICY — subject to the separately recorded human approval and exact evidence context shown in this report."
    if status == "READY_FOR_ENGINEERING_APPROVAL":
        return "ENGINEERING APPROVAL REQUIRED — deterministic gates passed to the configured threshold; final human approval remains required."
    if status == "CONDITIONALLY_READY":
        return "CONDITIONAL — complete the listed conditions and required FAT/evidence before release approval."
    if status == "BLOCKED":
        return "HOLD — one or more release blockers are present and must be dispositioned before commissioning/release approval."
    return "NOT READY — complete the listed engineering/FAT actions before release approval."


def _risk_counts(result) -> Counter:
    return Counter(item.severity.value for item in result.risks)


def _requirement_counts(result) -> Counter:
    return Counter(item.status for item in result.requirement_verification)


def _execution_counts(result) -> Counter:
    execution_by_test = {item.test_id: item.status for item in result.executions}
    counts = Counter()
    for test in result.engineering.fat_tests:
        counts[(execution_by_test.get(test.id) or ExecutionStatus.NOT_RUN).value] += 1
    return counts


def _top_risks(result, limit: int = 8):
    return sorted(
        result.risks,
        key=lambda item: (_SEVERITY_ORDER.get(item.severity, 99), item.category.casefold(), item.title.casefold()),
    )[:limit]


def render_professional_overview(result) -> str:
    """Render a customer-facing executive layer without removing engineering detail.

    This is presentation only. It does not change any verification, risk, FAT,
    requirement, or readiness verdict produced by deterministic analysis.
    """
    project = result.engineering.project
    readiness = result.readiness
    req = _requirement_counts(result)
    risks = _risk_counts(result)
    fat = _execution_counts(result)
    scenario_counts = Counter(test.scenario for test in result.engineering.fat_tests)
    finding_counts = Counter(item.severity.value for item in result.engineering_findings)

    proven_requirements = (
        req[RequirementStatus.STATICALLY_VERIFIED]
        + req[RequirementStatus.DYNAMICALLY_VERIFIED]
        + req[RequirementStatus.ACTION_EFFECT_PROVEN]
    )
    unresolved_requirements = (
        req[RequirementStatus.TRACEABLE_NOT_PROVEN]
        + req[RequirementStatus.NOT_MAPPED]
        + req[RequirementStatus.CONFLICT]
        + req[RequirementStatus.AI_CANDIDATE]
    )

    lines = [
        "## Executive Summary",
        "",
        "> **Report purpose:** Independent PLC engineering review, requirement traceability, logic-risk analysis, optimization recommendations, and engineer-executed FAT planning.",
        ">",
        f"> **Engineering outcome:** **{result.engineering.outcome.value}**",
        f"> **Release readiness:** **{readiness.status.value if readiness else 'NOT_EVALUATED'}**",
        f"> **Release recommendation:** **{_release_recommendation(result)}**",
        ">",
        "> **Execution boundary:** DevAgent analyzes and plans. It does not connect to, control, download to, or execute an external PLC/simulator/HIL environment. FAT execution remains an engineer-controlled activity.",
        "",
        "### Document Control",
        "",
    ]
    lines += _table(
        ["Field", "Value"],
        [
            ["Report", "DevAgent PLC Engineering Review & FAT Preparation Report"],
            ["Controller", project.metadata.controller_name],
            ["Processor", project.metadata.processor_type or "Not declared in export"],
            ["Engineering tool", project.metadata.engineering_tool],
            ["Source artifact", project.metadata.source_path],
            ["Source SHA-256", f"`{project.metadata.source_sha256}`"],
            ["Baseline SHA-256", f"`{result.baseline_sha256 or 'not supplied'}`"],
            ["Verification context SHA-256", f"`{result.verification_context_sha256 or 'not evaluated'}`"],
            ["Release policy", (result.release_policy or {}).get("policy_id", "not evaluated")],
        ],
    )

    lines += ["### Management Dashboard", ""]
    lines += _table(
        ["Review Area", "Result", "Management Interpretation"],
        [
            [
                "Engineering analysis",
                f"{len(project.tags)} tags / {len(project.routines)} routines / {len(result.engineering.graph.edges)} dependency edges",
                "Machine logic, dependencies, cause/effect, and supported sequencing evidence were analyzed within the declared semantic boundary.",
            ],
            [
                "Requirement verification",
                f"{proven_requirements} proven / {unresolved_requirements} unresolved of {len(result.requirement_verification)} mapped evaluations",
                "Unresolved or conflicting requirements remain visible and are not promoted to PASS.",
            ],
            [
                "Logic & risk review",
                f"CRITICAL {risks['CRITICAL']} / HIGH {risks['HIGH']} / MEDIUM {risks['MEDIUM']} / total {len(result.risks)}",
                "Prioritize critical/high findings, requirement conflicts, writer ownership, unreachable/contradictory logic, and suspicious sequencing before commissioning.",
            ],
            [
                "Optimization review",
                f"{len(result.optimizations)} evidence-backed candidate(s)",
                "Recommendations cover maintainability, simplification, duplication, ownership, and structural improvement; no automatic PLC edit is authorized.",
            ],
            [
                "FAT plan",
                f"{len(result.engineering.fat_tests)} procedure(s): PASS {fat['PASS']} / FAIL {fat['FAIL']} / NOT RUN {fat['NOT_RUN']} / BLOCKED {fat['BLOCKED']}",
                "Each generated FAT case retains source traceability, setup/actions, expected behavior, watch points, and required evidence.",
            ],
            [
                "Regression review",
                f"{len(result.regression_changes)} detected change-impact item(s)",
                "Affected requirements/tests remain traceable when a baseline is available.",
            ],
            [
                "Evidence package",
                f"{len(result.evidence)} evidence item(s); {len(result.verified_signatures)} verified signature(s)",
                "Detailed evidence and cryptographic context are preserved in later sections for auditability.",
            ],
        ],
    )

    lines += ["### Attention Summary", ""]
    lines += _table(
        ["Indicator", "Count"],
        [
            ["Critical engineering findings", finding_counts["CRITICAL"]],
            ["High engineering findings", finding_counts["HIGH"]],
            ["Critical risks", risks["CRITICAL"]],
            ["High risks", risks["HIGH"]],
            ["Requirement conflicts", req[RequirementStatus.CONFLICT]],
            ["Traceable but not proven requirements", req[RequirementStatus.TRACEABLE_NOT_PROVEN]],
            ["Unmapped requirements", req[RequirementStatus.NOT_MAPPED]],
            ["FAT procedures not yet run", fat["NOT_RUN"]],
            ["FAT failures", fat["FAIL"]],
            ["Release blockers", len(readiness.blockers) if readiness else 0],
        ],
    )

    lines += ["### Highest-Priority Logic / Risk Findings", ""]
    top_risks = _top_risks(result)
    lines += _table(
        ["Severity", "Category", "Finding", "Consequence", "Recommended Action"],
        [
            [item.severity.value, item.category, item.title, item.consequence, item.recommendation]
            for item in top_risks
        ],
    )

    lines += ["### Priority Engineering Actions", ""]
    priority_actions = sorted(
        result.recommendations,
        key=lambda item: (_SEVERITY_ORDER.get(item.priority, 99), item.title.casefold()),
    )[:8]
    if priority_actions:
        for index, item in enumerate(priority_actions, start=1):
            lines.append(
                f"{index}. **[{item.priority.value}] {item.title}** — {item.action}  "
                f"_Why:_ {item.rationale}"
            )
        lines.append("")
    else:
        lines += ["_No additional recommendation records were generated._", ""]

    if readiness and readiness.blockers:
        lines += ["### Release Blockers", ""]
        lines += [f"- **BLOCKER:** {item}" for item in readiness.blockers]
        lines.append("")

    lines += ["### FAT Readiness Snapshot", ""]
    lines += _table(
        ["Scenario", "Generated Procedures"],
        [[scenario, count] for scenario, count in sorted(scenario_counts.items())],
    )
    lines += [
        "**FAT execution status**",
        "",
        f"- PASS: **{fat['PASS']}**",
        f"- FAIL: **{fat['FAIL']}**",
        f"- NOT RUN: **{fat['NOT_RUN']}**",
        f"- BLOCKED: **{fat['BLOCKED']}**",
        "",
        "### Report Navigation",
        "",
        "1. Executive Summary and management dashboard",
        "2. Technical verification identity and 15-stage pipeline",
        "3. Four-core PLC review contract",
        "4. Semantic coverage / proof boundary",
        "5. Engineer FAT procedures",
        "6. Requirement verification and traceability",
        "7. Risk detection and optimization recommendations",
        "8. Regression analysis and engineering recommendations",
        "9. Release readiness, evidence, trust, and verification boundaries",
        "",
        "---",
        "",
        "## Technical Verification Identity",
        "",
        "> The following sections preserve the complete deterministic run identity, hashes, evidence references, detailed engineering findings, FAT procedures, and trust boundaries used to support the executive summary above.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_professional_overview"]
