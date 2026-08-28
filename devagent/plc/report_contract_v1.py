from __future__ import annotations

from collections import Counter

from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import ExecutionStatus, RequirementStatus, StageStatus


REPORT_CONTRACT_SCHEMA = "devagent-plc-report-contract-v1"

_STAGE_WEIGHT = {
    StageStatus.PASS: 1.0,
    StageStatus.PARTIAL: 0.6,
    StageStatus.BLOCKED: 0.0,
    StageStatus.NOT_RUN: 0.0,
}


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _engineering_analysis_score(result) -> float | None:
    """Score evidence-bearing review stages, excluding optional/not-applicable work.

    This is a report-completeness indicator only. It is intentionally separate
    from deterministic semantic proof and from release readiness.
    """

    required = {1, 2, 3, 4, 8, 10, 11, 13, 14}
    values: list[float] = []
    for stage in result.stages:
        if stage.number not in required or stage.status is StageStatus.SKIPPED:
            continue
        values.append(_STAGE_WEIGHT.get(stage.status, 0.0))
    if not values:
        return None
    return round(100.0 * sum(values) / len(values), 1)


def _fat_plan_score(result) -> float | None:
    tests = list(result.engineering.fat_tests)
    if not tests:
        return None
    fields = (
        lambda item: bool(item.expected),
        lambda item: bool(item.purpose or item.title),
        lambda item: bool(item.setup_steps),
        lambda item: bool(item.action_steps),
        lambda item: bool(item.watch_tags or item.output_tag),
        lambda item: bool(item.evidence_required),
        lambda item: bool(item.failure_implication),
    )
    present = sum(bool(check(test)) for test in tests for check in fields)
    return round(100.0 * present / (len(tests) * len(fields)), 1)


def _semantic_counts(result) -> dict[str, int | float | None]:
    statements = list(result.engineering.project.logic_statements)
    counts = Counter(item.semantic_state for item in statements)
    full = counts[PLCSemanticState.FULL]
    partial = counts[PLCSemanticState.PARTIAL]
    opaque = counts[PLCSemanticState.OPAQUE]
    total = len(statements)
    return {
        "normalized_logic_objects": total,
        "full": full,
        "partial": partial,
        "opaque": opaque,
        "full_pct": _pct(full, total),
    }


def _requirement_summary(result) -> dict[str, object]:
    if not result.requirements:
        return {
            "scope": "NOT_PROVIDED",
            "total": 0,
            "proven": 0,
            "unresolved": 0,
            "conflicts": 0,
        }
    counts = Counter(item.status for item in result.requirement_verification)
    proven = (
        counts[RequirementStatus.STATICALLY_VERIFIED]
        + counts[RequirementStatus.DYNAMICALLY_VERIFIED]
        + counts[RequirementStatus.ACTION_EFFECT_PROVEN]
    )
    conflicts = counts[RequirementStatus.CONFLICT]
    return {
        "scope": "EVALUATED",
        "total": len(result.requirements),
        "proven": proven,
        "unresolved": max(0, len(result.requirements) - proven),
        "conflicts": conflicts,
    }


def _execution_summary(result) -> dict[str, object]:
    tests = list(result.engineering.fat_tests)
    by_test = {item.test_id: item.status for item in result.executions}
    counts = Counter((by_test.get(test.id) or ExecutionStatus.NOT_RUN).value for test in tests)
    executed = counts[ExecutionStatus.PASS.value] + counts[ExecutionStatus.FAIL.value] + counts[ExecutionStatus.BLOCKED.value]
    return {
        "total": len(tests),
        "executed": executed,
        "executed_pct": _pct(executed, len(tests)),
        "pass": counts[ExecutionStatus.PASS.value],
        "fail": counts[ExecutionStatus.FAIL.value],
        "blocked": counts[ExecutionStatus.BLOCKED.value],
        "not_run": counts[ExecutionStatus.NOT_RUN.value],
    }


def report_contract_violations(result) -> tuple[str, ...]:
    """Detect report contradictions without changing engineering verdicts."""

    violations: list[str] = []
    if not result.requirements:
        requirement_risks = [
            item.id
            for item in result.risks
            if item.category.casefold() in {"requirement", "requirement_coverage"}
        ]
        if requirement_risks:
            violations.append(
                "Customer requirements were not supplied, but requirement-coverage risk records exist: "
                + ", ".join(requirement_risks[:8])
            )

    if not result.executions:
        for test in result.engineering.fat_tests:
            if str(test.execution_status).upper() == "PASS":
                violations.append(
                    f"FAT assertion {test.id} claims PASS without imported execution evidence."
                )
                break

    semantic = _semantic_counts(result)
    if result.engineering.outcome.value == "STATICALLY_VERIFIED" and (
        semantic["partial"] or semantic["opaque"]
    ):
        violations.append(
            "Engineering outcome is STATICALLY_VERIFIED while normalized logic still contains PARTIAL/OPAQUE objects."
        )
    return tuple(violations)


def build_report_contract(result) -> dict[str, object]:
    semantic = _semantic_counts(result)
    requirements = _requirement_summary(result)
    execution = _execution_summary(result)
    readiness = result.readiness
    violations = report_contract_violations(result)
    return {
        "schema": REPORT_CONTRACT_SCHEMA,
        "review_mode": (
            "PROJECT_ONLY_ENGINEERING_REVIEW"
            if requirements["scope"] == "NOT_PROVIDED"
            else "REQUIREMENT_VERIFICATION_REVIEW"
        ),
        "engineering_analysis_score": _engineering_analysis_score(result),
        "semantic": semantic,
        "requirements": requirements,
        "fat_plan_completeness_score": _fat_plan_score(result),
        "execution": execution,
        "evidence_items": len(result.evidence),
        "verified_signatures": len(result.verified_signatures),
        "release": {
            "status": readiness.status.value if readiness else "NOT_EVALUATED",
            "score": readiness.score if readiness else None,
            "blockers": len(readiness.blockers) if readiness else 0,
        },
        "contract_status": "PASS" if not violations else "ATTENTION_REQUIRED",
        "violations": violations,
        "release_score_is_not_analysis_quality": True,
    }


__all__ = [
    "REPORT_CONTRACT_SCHEMA",
    "build_report_contract",
    "report_contract_violations",
]
