from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import (
    ExecutionStatus,
    ReadinessStatus,
    ReleaseReadiness,
    RequirementStatus,
    RequirementVerificationMode,
    RiskFinding,
    Severity,
)


def load_approval(
    path: Path | None,
    *,
    project_sha256: str,
    test_plan_sha256: str,
    requirements_sha256: str,
    backend_registry_sha256: str | None,
    baseline_sha256: str | None,
    verification_context_sha256: str,
) -> dict[str, Any] | None:
    if path is None:
        return None
    target = path.expanduser().resolve(strict=True)
    if target.stat().st_size > 1024 * 1024:
        raise ValueError("Approval artifact exceeds 1 MiB production limit")
    loaded = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("Approval artifact must be a JSON object")
    expected = {
        "project_sha256": project_sha256,
        "test_plan_sha256": test_plan_sha256,
        "requirements_sha256": requirements_sha256,
        "backend_registry_sha256": backend_registry_sha256,
        "baseline_sha256": baseline_sha256,
        "verification_context_sha256": verification_context_sha256,
    }
    for field, value in expected.items():
        if loaded.get(field) != value:
            raise ValueError(f"Approval {field} does not match the current PLC verification context")
    if str(loaded.get("decision", "")).upper() != "APPROVE":
        raise ValueError("Approval artifact decision must be APPROVE")
    if not str(loaded.get("approved_by", "")).strip() or not str(loaded.get("approved_at", "")).strip():
        raise ValueError("Approval artifact requires approved_by and approved_at")
    return {
        "decision": "APPROVE",
        "approved_by": str(loaded["approved_by"]),
        "approved_at": str(loaded["approved_at"]),
        **expected,
        "source_path": str(target),
    }


def _requirement_release_gaps(requirements, verifications) -> list[str]:
    req_by_id = {item.id: item for item in requirements}
    gaps: list[str] = []
    for verification in verifications:
        requirement = req_by_id.get(verification.requirement_id)
        if requirement is None:
            gaps.append(verification.requirement_id)
            continue
        if requirement.verification_mode is RequirementVerificationMode.STATIC:
            accepted = verification.status in {
                RequirementStatus.STATICALLY_VERIFIED,
                RequirementStatus.DYNAMICALLY_VERIFIED,
            }
        else:
            accepted = verification.status is RequirementStatus.DYNAMICALLY_VERIFIED
        if not accepted:
            gaps.append(verification.requirement_id)
    return gaps


def evaluate_release_readiness(
    engineering,
    requirements,
    verifications,
    tests,
    executions,
    risks: list[RiskFinding],
    regression_changes,
    approval: dict[str, Any] | None,
) -> ReleaseReadiness:
    blockers: list[str] = []
    conditions: list[str] = []
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        blockers.append("PLC semantic coverage is incomplete; one or more behaviors remain PARTIAL/NOT_PROVEN.")
    if not requirements:
        blockers.append("No customer/engineering requirements were supplied, so requirement coverage cannot be proven.")

    release_gaps = _requirement_release_gaps(requirements, verifications)
    if release_gaps:
        dynamic_gaps = sum(
            1
            for requirement_id in release_gaps
            if next((item for item in requirements if item.id == requirement_id), None)
            and next(item for item in requirements if item.id == requirement_id).verification_mode
            is RequirementVerificationMode.DYNAMIC
        )
        blockers.append(
            f"{len(release_gaps)} requirement(s) do not satisfy their release verification policy"
            + (f"; {dynamic_gaps} require qualified-backend dynamic PASS evidence." if dynamic_gaps else ".")
        )

    statuses = {item.test_id: item.status for item in executions}
    if tests:
        failed = [test.id for test in tests if statuses.get(test.id) is ExecutionStatus.FAIL]
        missing = [test.id for test in tests if statuses.get(test.id) is not ExecutionStatus.PASS]
        if failed:
            blockers.append(f"{len(failed)} generated FAT test(s) failed execution.")
        elif missing:
            blockers.append(f"{len(missing)} generated FAT test(s) do not have PASS execution evidence.")
    else:
        blockers.append("No executable FAT candidates were generated for the normalized logic.")

    deterministic_high = [
        risk
        for risk in risks
        if risk.origin == "DETERMINISTIC" and risk.severity in {Severity.CRITICAL, Severity.HIGH}
    ]
    if deterministic_high:
        blockers.append(f"{len(deterministic_high)} unresolved deterministic HIGH/CRITICAL risk(s) remain.")
    medium = [
        risk
        for risk in risks
        if risk.origin == "DETERMINISTIC" and risk.severity is Severity.MEDIUM
    ]
    if medium:
        conditions.append(f"Disposition {len(medium)} deterministic MEDIUM risk(s).")
    ai_high = [
        risk
        for risk in risks
        if risk.origin != "DETERMINISTIC" and risk.severity in {Severity.HIGH, Severity.CRITICAL}
    ]
    if ai_high:
        conditions.append(f"Human-review {len(ai_high)} AI risk candidate(s); AI findings are not treated as proof.")

    impacted = sorted({test for change in regression_changes for test in change.affected_test_ids})
    if impacted:
        passed = {item.test_id for item in executions if item.status is ExecutionStatus.PASS}
        missing_impacted = [test for test in impacted if test not in passed]
        if missing_impacted:
            blockers.append(f"{len(missing_impacted)} regression-impacted test(s) lack PASS execution evidence.")

    score = 100
    score -= 25 if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED else 0
    score -= 25 if not requirements else 0
    score -= min(30, 6 * len(release_gaps))
    if not tests:
        score -= 20
    else:
        failed_count = sum(1 for test in tests if statuses.get(test.id) is ExecutionStatus.FAIL)
        missing_count = sum(1 for test in tests if statuses.get(test.id) is not ExecutionStatus.PASS)
        score -= min(35, 15 * failed_count + 5 * max(0, missing_count - failed_count))
    score -= min(20, 5 * len(deterministic_high))
    score -= min(10, 2 * len(medium))
    if impacted:
        passed = {item.test_id for item in executions if item.status is ExecutionStatus.PASS}
        score -= min(10, 2 * sum(1 for test in impacted if test not in passed))
    score = max(0, min(100, score))

    has_critical = (
        any(risk.origin == "DETERMINISTIC" and risk.severity is Severity.CRITICAL for risk in risks)
        or any(item.status is ExecutionStatus.FAIL for item in executions)
    )
    if blockers:
        status = ReadinessStatus.BLOCKED if has_critical else ReadinessStatus.NOT_READY
    elif conditions:
        status = ReadinessStatus.CONDITIONALLY_READY
    else:
        status = ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL
    if status is ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL and approval:
        status = ReadinessStatus.APPROVED_FOR_RELEASE

    summary = {
        ReadinessStatus.BLOCKED: "Release is blocked by failed/critical evidence.",
        ReadinessStatus.NOT_READY: "Evidence package is incomplete for release.",
        ReadinessStatus.CONDITIONALLY_READY: "Core gates passed, but engineering conditions still require disposition.",
        ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL: "Automated evidence gates passed; human engineering approval is still required.",
        ReadinessStatus.APPROVED_FOR_RELEASE: "Automated evidence gates passed and a matching human approval artifact was supplied.",
    }[status]
    metrics = {
        "requirements_total": len(requirements),
        "requirements_dynamic_policy": sum(
            1 for item in requirements if item.verification_mode is RequirementVerificationMode.DYNAMIC
        ),
        "requirements_static_policy": sum(
            1 for item in requirements if item.verification_mode is RequirementVerificationMode.STATIC
        ),
        "requirements_dynamic_verified": sum(
            1 for item in verifications if item.status is RequirementStatus.DYNAMICALLY_VERIFIED
        ),
        "requirements_static_verified": sum(
            1 for item in verifications if item.status is RequirementStatus.STATICALLY_VERIFIED
        ),
        "requirements_release_gaps": len(release_gaps),
        "tests_total": len(tests),
        "tests_passed": sum(1 for item in executions if item.status is ExecutionStatus.PASS),
        "tests_failed": sum(1 for item in executions if item.status is ExecutionStatus.FAIL),
        "risks_critical": sum(1 for item in risks if item.severity is Severity.CRITICAL),
        "risks_high": sum(1 for item in risks if item.severity is Severity.HIGH),
        "risks_medium": sum(1 for item in risks if item.severity is Severity.MEDIUM),
        "regression_changes": len(regression_changes),
        "regression_impacted_tests": len(impacted),
        "static_outcome": engineering.outcome.value,
    }
    return ReleaseReadiness(
        status,
        score,
        summary,
        tuple(blockers),
        tuple(conditions),
        metrics,
        True,
        approval,
    )
