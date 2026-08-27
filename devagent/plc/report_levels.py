from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path

from devagent.plc.production_models import (
    ExecutionStatus,
    PLCProductionResult,
    RequirementStatus,
    Severity,
)


_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
}

_SCENARIO_ORDER = {
    "REQUIREMENT": 0,
    "NEGATIVE_PATH": 1,
    "STATE_TRANSITION_RUNTIME": 2,
    "STATEFUL_RUNTIME": 3,
    "MOTION_RUNTIME": 4,
    "ACTION_PATH": 5,
    "POSITIVE_PATH": 6,
}


def _ordered_unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _execution_counts(result: PLCProductionResult) -> Counter[str]:
    execution_by_test = {item.test_id: item.status for item in result.executions}
    counts: Counter[str] = Counter()
    for test in result.engineering.fat_tests:
        status = execution_by_test.get(test.id) or ExecutionStatus.NOT_RUN
        counts[status.value] += 1
    return counts


def _requirement_counts(result: PLCProductionResult) -> Counter[RequirementStatus]:
    return Counter(item.status for item in result.requirement_verification)


def _risk_counts(result: PLCProductionResult) -> Counter[str]:
    return Counter(item.severity.value for item in result.risks)


def _scenario_label(value: str) -> str:
    return value.replace("_", " ").strip().title() or "General PLC Behavior"


def _scenario_owner(test) -> tuple[str, str]:
    source = test.source
    owner = source.aoi or source.program or source.routine or source.controller or "PROJECT"
    routine = source.routine or ""
    return str(owner), str(routine)


def _common_steps(tests, attribute: str, *, limit: int = 6) -> list[str]:
    if not tests:
        return []
    first = _ordered_unique(getattr(tests[0], attribute, ()))
    if len(tests) == 1:
        return first[:limit]
    common = {item.casefold() for item in first}
    for test in tests[1:]:
        common &= {str(item).strip().casefold() for item in getattr(test, attribute, ()) if str(item).strip()}
    return [item for item in first if item.casefold() in common][:limit]


def build_fat_scenario_index(result: PLCProductionResult) -> list[dict[str, object]]:
    """Roll low-level FAT assertions into deterministic engineer work packages.

    This is presentation-only grouping. Every original FAT test remains authoritative,
    individually hash-bound, and available in the detailed plan. Grouping never changes
    PASS/FAIL/readiness semantics and never claims that one grouped procedure proves
    behavior that the underlying assertions did not model.
    """

    groups: dict[tuple[str, str, str], list[object]] = defaultdict(list)
    for test in result.engineering.fat_tests:
        owner, routine = _scenario_owner(test)
        groups[(owner.casefold(), routine.casefold(), test.scenario.casefold())].append(test)

    ranked = sorted(
        groups.items(),
        key=lambda item: (
            _SCENARIO_ORDER.get(item[1][0].scenario, 99),
            -len(item[1]),
            item[0],
        ),
    )
    vendor = str(result.engineering.project.metadata.vendor or "PLC").upper().replace(" ", "-")
    scenarios: list[dict[str, object]] = []
    for number, (_key, tests) in enumerate(ranked, start=1):
        representative = tests[0]
        owner, routine = _scenario_owner(representative)
        scope = owner if not routine or routine.casefold() == owner.casefold() else f"{owner} / {routine}"
        test_ids = [test.id for test in tests]
        outputs = _ordered_unique(test.output_tag for test in tests)
        purposes = _ordered_unique(test.purpose for test in tests)
        expected = _ordered_unique(test.expected for test in tests)
        watch_tags = _ordered_unique(tag for test in tests for tag in test.watch_tags)
        evidence_required = _ordered_unique(
            item for test in tests for item in test.evidence_required
        )
        source_locators = _ordered_unique(test.source.locator for test in tests)
        common_setup = _common_steps(tests, "setup_steps")
        common_actions = _common_steps(tests, "action_steps")
        scenario_id = f"FAT-{vendor}-{number:03d}"
        scenarios.append(
            {
                "id": scenario_id,
                "title": f"{_scenario_label(representative.scenario)} — {scope}",
                "scenario": representative.scenario,
                "scope": scope,
                "low_level_assertion_count": len(tests),
                "test_ids": test_ids,
                "outputs": outputs,
                "watch_tags": watch_tags,
                "source_locators": source_locators,
                "purposes": purposes,
                "expected_examples": expected,
                "common_setup_steps": common_setup,
                "common_action_steps": common_actions,
                "evidence_required": evidence_required,
                "engineer_execution_required": any(test.engineer_execution_required for test in tests),
            }
        )
    return scenarios


def render_fat_scenarios_markdown(
    result: PLCProductionResult,
    *,
    limit: int = 20,
) -> str:
    scenarios = build_fat_scenario_index(result)
    selected = scenarios[: max(0, limit)]
    lines = [
        "# DevAgent Engineer FAT Scenario Work Packages",
        "",
        "> This Level 1/2 view groups source-traceable low-level FAT assertions into practical engineer work packages. It does not remove, merge, or alter the underlying acceptance assertions. The complete mapping remains in `fat_scenario_index.json` and `fat_tests.csv`.",
        "",
        f"- Low-level FAT assertions preserved: **{len(result.engineering.fat_tests)}**",
        f"- Deterministic scenario groups discovered: **{len(scenarios)}**",
        f"- Prioritized engineer scenarios shown here: **{len(selected)}**",
        "",
    ]
    if not selected:
        lines += ["_No FAT scenarios were generated._", ""]
        return "\n".join(lines)

    for scenario in selected:
        lines += [
            f"## {scenario['id']} — {scenario['title']}",
            "",
            f"**Coverage:** {scenario['low_level_assertion_count']} low-level assertion(s)",
            "",
            f"**Source scope:** {scenario['scope']}",
            "",
        ]
        purposes = scenario["purposes"]
        if purposes:
            lines += ["**Objective**", ""]
            for item in purposes[:3]:
                lines.append(f"- {item}")
            if len(purposes) > 3:
                lines.append(f"- … {len(purposes) - 3} additional source-traceable objectives")
            lines.append("")

        setup = scenario["common_setup_steps"]
        lines += ["**Preconditions / setup**", ""]
        if setup:
            lines += [f"- {item}" for item in setup]
        lines.append("- Apply the case-specific preconditions from `fat_tests.csv`; incompatible case preconditions are not combined into one machine state.")
        lines.append("")

        actions = scenario["common_action_steps"]
        lines += ["**Engineer procedure**", ""]
        if actions:
            lines += [f"{index}. {item}" for index, item in enumerate(actions, start=1)]
        else:
            lines += [
                "1. Confirm the PLC revision under test matches the analyzed source.",
                "2. Place the simulator/HIL/test bench or PLC in an approved controlled test condition.",
                "3. Execute the covered detailed cases using their source-linked preconditions.",
                "4. Observe the listed outputs/watch tags through the relevant PLC scan or sequence.",
                "5. Record PASS/FAIL only against each underlying expected result and preserve the required evidence.",
            ]
        lines.append("")

        outputs = scenario["outputs"]
        watches = scenario["watch_tags"]
        lines += ["**Observe**", ""]
        lines.append(f"- Outputs: {', '.join(outputs[:12]) or 'See detailed FAT cases'}")
        if len(outputs) > 12:
            lines.append(f"- Additional outputs: {len(outputs) - 12}")
        lines.append(f"- Watch tags: {', '.join(watches[:16]) or 'See detailed FAT cases'}")
        if len(watches) > 16:
            lines.append(f"- Additional watch tags: {len(watches) - 16}")
        lines.append("")

        expected = scenario["expected_examples"]
        lines += ["**Expected acceptance evidence**", ""]
        for item in expected[:5]:
            lines.append(f"- {item}")
        if len(expected) > 5:
            lines.append(f"- … {len(expected) - 5} additional assertion-specific expected result(s) in `fat_tests.csv`")
        lines.append("")

        required = scenario["evidence_required"]
        if required:
            lines += ["**Evidence to retain**", ""]
            lines += [f"- {item}" for item in required[:6]]
            if len(required) > 6:
                lines.append(f"- … {len(required) - 6} additional evidence requirement(s)")
            lines.append("")

        ids = scenario["test_ids"]
        lines += ["**Underlying assertions**", ""]
        lines.append(", ".join(ids[:10]))
        if len(ids) > 10:
            lines.append(f"… plus {len(ids) - 10} additional assertion(s); see `fat_scenario_index.json`.")
        lines += ["", "---", ""]

    if len(scenarios) > len(selected):
        lines += [
            f"Additional scenario groups not shown in this prioritized view: **{len(scenarios) - len(selected)}**.",
            "",
            "Use `fat_scenario_index.json` for the complete grouped mapping and `fat_tests.csv` / `fat_tests.json` for every low-level acceptance assertion.",
            "",
        ]
    return "\n".join(lines)


def render_fat_tests_csv(result: PLCProductionResult) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "test_id",
            "scenario",
            "purpose",
            "source",
            "output_tag",
            "preconditions_json",
            "expected",
            "execution_status",
            "method",
            "limitations",
            "setup_steps",
            "action_steps",
            "watch_tags",
            "evidence_required",
            "why_required",
            "failure_implication",
            "recommended_environment",
        ]
    )
    execution_by_test = {item.test_id: item.status.value for item in result.executions}
    for test in result.engineering.fat_tests:
        writer.writerow(
            [
                test.id,
                test.scenario,
                test.purpose,
                test.source.locator,
                test.output_tag,
                json.dumps(test.preconditions, sort_keys=True),
                test.expected,
                execution_by_test.get(test.id, ExecutionStatus.NOT_RUN.value),
                test.method,
                " ; ".join(test.limitations),
                " ; ".join(test.setup_steps),
                " ; ".join(test.action_steps),
                " ; ".join(test.watch_tags),
                " ; ".join(test.evidence_required),
                test.why_required,
                test.failure_implication,
                test.recommended_environment,
            ]
        )
    return stream.getvalue()


def render_risk_register_csv(result: PLCProductionResult) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "risk_id",
            "severity",
            "origin",
            "category",
            "title",
            "summary",
            "consequence",
            "recommendation",
            "evidence_ids",
        ]
    )
    for item in sorted(
        result.risks,
        key=lambda risk: (
            _SEVERITY_ORDER.get(risk.severity.value, 99),
            risk.category.casefold(),
            risk.title.casefold(),
        ),
    ):
        writer.writerow(
            [
                item.id,
                item.severity.value,
                item.origin,
                item.category,
                item.title,
                item.summary,
                item.consequence,
                item.recommendation,
                " ; ".join(item.evidence_ids),
            ]
        )
    return stream.getvalue()


def render_optimization_report_markdown(result: PLCProductionResult) -> str:
    lines = [
        "# DevAgent PLC Optimization Review",
        "",
        "> Advisory engineering recommendations only. DevAgent does not modify PLC code through this report.",
        "",
        f"Optimization candidates: **{len(result.optimizations)}**",
        "",
    ]
    if not result.optimizations:
        lines += ["_No optimization candidates were generated._", ""]
        return "\n".join(lines)
    for item in result.optimizations:
        lines += [
            f"## {item.id} — {item.category}",
            "",
            f"**Change risk:** {item.change_risk.value}",
            "",
            f"**Current:** {item.current_state}",
            "",
            f"**Proposed:** {item.proposed_change}",
            "",
            f"**Expected benefit:** {item.expected_benefit}",
            "",
            f"**Evidence:** {', '.join(item.evidence_ids) or 'none'}",
            "",
        ]
    return "\n".join(lines)


def _top_findings(result: PLCProductionResult, *, limit: int = 5) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for item in result.risks:
        candidates.append(
            (
                _SEVERITY_ORDER.get(item.severity.value, 99),
                0,
                f"[{item.severity.value}] {item.title}",
            )
        )
    for item in result.engineering_findings:
        candidates.append(
            (
                _SEVERITY_ORDER.get(item.severity.value, 99),
                1,
                f"[{item.severity.value}] {item.title}",
            )
        )
    result_lines: list[str] = []
    seen: set[str] = set()
    for _severity, _kind, text in sorted(candidates, key=lambda item: (item[0], item[1], item[2].casefold())):
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result_lines.append(text)
        if len(result_lines) >= limit:
            break
    return result_lines


def _priority_recommendations(result: PLCProductionResult, *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    fat = _execution_counts(result)
    risks = _risk_counts(result)
    req = _requirement_counts(result)

    derived: list[str] = []
    if fat[ExecutionStatus.NOT_RUN.value]:
        derived.append("Execute the prioritized FAT scenario work packages in an engineer-controlled qualified runtime environment and import authenticated results.")
    if risks["CRITICAL"] or risks["HIGH"]:
        derived.append("Disposition deterministic CRITICAL/HIGH risks before commissioning or release approval.")
    if result.engineering.outcome.value != "STATICALLY_VERIFIED":
        derived.append("Review PARTIAL/OPAQUE semantic coverage and resolve or FAT-test unsupported source regions; do not treat them as silently proven.")
    unresolved = (
        req[RequirementStatus.TRACEABLE_NOT_PROVEN]
        + req[RequirementStatus.NOT_MAPPED]
        + req[RequirementStatus.CONFLICT]
        + req[RequirementStatus.AI_CANDIDATE]
    )
    if unresolved:
        derived.append("Close unresolved requirement proof gaps with source traceability and required runtime acceptance evidence.")

    ordered_recommendations = sorted(
        result.recommendations,
        key=lambda item: (
            _SEVERITY_ORDER.get(item.priority.value, 99),
            item.title.casefold(),
        ),
    )
    candidates = [*derived, *(f"{item.title} — {item.action}" for item in ordered_recommendations)]
    for text in candidates:
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def render_console_summary(
    result: PLCProductionResult,
    *,
    output_dir: Path | None = None,
    scenario_limit: int = 20,
) -> str:
    """Level 1 terminal report: concise, decision-oriented, and non-lossy by reference."""

    project = result.engineering.project
    readiness = result.readiness
    req = _requirement_counts(result)
    fat = _execution_counts(result)
    risks = _risk_counts(result)
    scenario_count = len(build_fat_scenario_index(result))
    proven = (
        req[RequirementStatus.STATICALLY_VERIFIED]
        + req[RequirementStatus.DYNAMICALLY_VERIFIED]
        + req[RequirementStatus.ACTION_EFFECT_PROVEN]
    )
    declared_dynamic = {
        requirement.id
        for requirement in result.requirements
        if requirement.verification_mode.value == "DYNAMIC"
    }
    dynamic_proven = {
        verification.requirement_id
        for verification in result.requirement_verification
        if verification.status is RequirementStatus.DYNAMICALLY_VERIFIED
    }
    runtime_needed = len(declared_dynamic - dynamic_proven)
    release_gaps = (
        int(readiness.metrics.get("requirements_release_gaps", 0))
        if readiness is not None
        else 0
    )

    lines = [
        "DEVAGENT PLC ENGINEERING REVIEW",
        f"Project: {project.metadata.controller_name}",
        f"Platform: {project.metadata.vendor} / {project.metadata.engineering_tool}",
        "",
        "STATUS",
        result.engineering.outcome.value,
        "",
        "RELEASE READINESS",
        f"{readiness.status.value if readiness else 'NOT_EVALUATED'} — {readiness.score if readiness else 0}/100",
        "",
        "SUMMARY",
        f"Requirements analyzed:          {len(result.requirements)}",
        f"Requirements proven:            {proven}",
        f"Requirements needing runtime:   {runtime_needed}",
        f"Requirements release gaps:      {release_gaps}",
        "",
        f"FAT assertions generated:       {len(result.engineering.fat_tests)}",
        f"Engineer FAT scenario groups:   {scenario_count}",
        f"Prioritized scenarios exported: {min(scenario_limit, scenario_count)}",
        f"FAT assertions executed:        {len(result.executions)}",
        f"Passed:                         {fat[ExecutionStatus.PASS.value]}",
        f"Failed:                         {fat[ExecutionStatus.FAIL.value]}",
        f"Blocked:                        {fat[ExecutionStatus.BLOCKED.value]}",
        f"Not run:                        {fat[ExecutionStatus.NOT_RUN.value]}",
        "",
        "RISKS",
        f"Critical: {risks['CRITICAL']}",
        f"High:     {risks['HIGH']}",
        f"Medium:   {risks['MEDIUM']}",
        f"Low:      {risks['LOW']}",
        "",
        "TOP FINDINGS",
    ]
    findings = _top_findings(result)
    lines += [f"{index}. {item}" for index, item in enumerate(findings, start=1)] or ["1. No priority finding records generated."]
    lines += ["", "TOP RECOMMENDATIONS"]
    recommendations = _priority_recommendations(result)
    lines += [f"{index}. {item}" for index, item in enumerate(recommendations, start=1)] or ["1. No additional recommendations generated."]
    lines += ["", "REPORT LEVELS"]
    if output_dir is not None:
        lines += [
            f"Level 1 — Console summary:       {output_dir / 'report_summary.txt'}",
            f"Level 2 — Full engineering:      {output_dir / 'fat_report.md'}",
            f"Level 2 — FAT scenarios:         {output_dir / 'fat_scenarios.md'}",
            f"Level 2 — FAT assertion detail:  {output_dir / 'fat_tests.csv'}",
            f"Level 2 — Risk register:         {output_dir / 'risk_register.csv'}",
            f"Level 2 — Optimization detail:   {output_dir / 'optimization_report.md'}",
            f"Level 3 — Raw evidence:          {output_dir / 'evidence_manifest.json'}",
            f"Level 3 — Canonical PLC IR:      {output_dir / 'canonical_ir.json'}",
        ]
    else:
        lines += [
            "Level 1 — This console summary",
            "Level 2/3 — Not written because --no-write was selected",
        ]
    lines += [
        "",
        "Use --full-report to print the complete engineering report in the terminal.",
        "Deep analysis and raw evidence are preserved even when the terminal view is concise.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "build_fat_scenario_index",
    "render_console_summary",
    "render_fat_scenarios_markdown",
    "render_fat_tests_csv",
    "render_optimization_report_markdown",
    "render_risk_register_csv",
]
