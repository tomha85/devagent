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
from devagent.plc.release_policy import PLCReleasePolicy


def load_approval_v5(
    path: Path | None,
    *,
    project_sha256: str,
    test_plan_sha256: str,
    requirements_sha256: str,
    backend_registry_sha256: str | None,
    baseline_sha256: str | None,
    execution_results_sha256: str | None,
    release_policy_sha256: str,
    trust_store_sha256: str | None,
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
        "execution_results_sha256": execution_results_sha256,
        "release_policy_sha256": release_policy_sha256,
        "trust_store_sha256": trust_store_sha256,
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


def _requirement_release_gaps(requirements, verifications, policy: PLCReleasePolicy) -> list[str]:
    req_by_id = {item.id: item for item in requirements}
    gaps: list[str] = []
    for verification in verifications:
        requirement = req_by_id.get(verification.requirement_id)
        if requirement is None:
            gaps.append(verification.requirement_id)
            continue
        dynamic_required = (
            requirement.verification_mode is RequirementVerificationMode.DYNAMIC
            or requirement.criticality in policy.require_dynamic_for
        )
        if dynamic_required:
            accepted = verification.status is RequirementStatus.DYNAMICALLY_VERIFIED
        else:
            accepted = verification.status in {
                RequirementStatus.STATICALLY_VERIFIED,
                RequirementStatus.DYNAMICALLY_VERIFIED,
            }
        if not accepted:
            gaps.append(verification.requirement_id)
    return gaps


def evaluate_release_readiness_v5(
    engineering,
    requirements,
    verifications,
    tests,
    executions,
    risks: list[RiskFinding],
    regression_changes,
    approval: dict[str, Any] | None,
    *,
    policy: PLCReleasePolicy,
    baseline_sha256: str | None,
    execution_backend_kind: str | None,
) -> ReleaseReadiness:
    blockers: list[str] = []
    conditions: list[str] = []
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        blockers.append("PLC semantic coverage is incomplete; one or more behaviors remain PARTIAL/NOT_PROVEN.")
    if not requirements:
        blockers.append("No customer/engineering requirements were supplied, so requirement coverage cannot be proven.")

    release_gaps = _requirement_release_gaps(requirements, verifications, policy)
    if release_gaps:
        dynamic_gaps = sum(
            1
            for requirement_id in release_gaps
            for requirement in [next((item for item in requirements if item.id == requirement_id), None)]
            if requirement is not None
            and (
                requirement.verification_mode is RequirementVerificationMode.DYNAMIC
                or requirement.criticality in policy.require_dynamic_for
            )
        )
        blockers.append(
            f"{len(release_gaps)} requirement(s) do not satisfy release policy {policy.policy_id}"
            + (f"; {dynamic_gaps} require qualified-backend dynamic PASS evidence." if dynamic_gaps else ".")
        )

    baseline_required = [item.id for item in requirements if item.criticality in policy.require_baseline_for]
    if baseline_required and baseline_sha256 is None:
        blockers.append(
            f"Release policy requires a regression baseline for {len(baseline_required)} requirement(s) at configured criticality levels."
        )

    statuses = {item.test_id: item.status for item in executions}
    if policy.require_all_generated_tests_pass:
        if tests:
            failed = [test.id for test in tests if statuses.get(test.id) is ExecutionStatus.FAIL]
            missing = [test.id for test in tests if statuses.get(test.id) is not ExecutionStatus.PASS]
            if failed:
                blockers.append(f"{len(failed)} generated FAT test(s) failed execution.")
            elif missing:
                blockers.append(f"{len(missing)} generated FAT test(s) do not have PASS execution evidence.")
        else:
            blockers.append("No executable FAT candidates were generated for the normalized logic.")

    if executions and execution_backend_kind not in policy.allowed_backend_kinds:
        blockers.append(
            f"Execution backend kind {execution_backend_kind or '<unknown>'} is not allowed by release policy {policy.policy_id}."
        )

    deterministic_counts = {
        Severity.CRITICAL: sum(1 for risk in risks if risk.origin == "DETERMINISTIC" and risk.severity is Severity.CRITICAL),
        Severity.HIGH: sum(1 for risk in risks if risk.origin == "DETERMINISTIC" and risk.severity is Severity.HIGH),
        Severity.MEDIUM: sum(1 for risk in risks if risk.origin == "DETERMINISTIC" and risk.severity is Severity.MEDIUM),
    }
    budgets = {
        Severity.CRITICAL: policy.max_deterministic_critical,
        Severity.HIGH: policy.max_deterministic_high,
        Severity.MEDIUM: policy.max_deterministic_medium,
    }
    for severity, count in deterministic_counts.items():
        if count > budgets[severity]:
            blockers.append(f"{count} unresolved deterministic {severity.value} risk(s) exceed policy limit {budgets[severity]}.")
        elif count:
            conditions.append(f"Disposition {count} deterministic {severity.value} risk(s) before final engineering signoff.")

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
    score -= 15 if baseline_required and baseline_sha256 is None else 0
    if policy.require_all_generated_tests_pass:
        if not tests:
            score -= 20
        else:
            failed_count = sum(1 for test in tests if statuses.get(test.id) is ExecutionStatus.FAIL)
            missing_count = sum(1 for test in tests if statuses.get(test.id) is not ExecutionStatus.PASS)
            score -= min(35, 15 * failed_count + 5 * max(0, missing_count - failed_count))
    for severity, count in deterministic_counts.items():
        excess = max(0, count - budgets[severity])
        weight = {Severity.CRITICAL: 15, Severity.HIGH: 8, Severity.MEDIUM: 3}[severity]
        score -= min(30, weight * excess)
    if impacted:
        passed = {item.test_id for item in executions if item.status is ExecutionStatus.PASS}
        score -= min(10, 2 * sum(1 for test in impacted if test not in passed))
    score = max(0, min(100, score))

    has_critical = (
        deterministic_counts[Severity.CRITICAL] > policy.max_deterministic_critical
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
        ReadinessStatus.BLOCKED: "Release is blocked by failed/critical evidence or release-policy violation.",
        ReadinessStatus.NOT_READY: "Evidence package is incomplete for the configured release policy.",
        ReadinessStatus.CONDITIONALLY_READY: "Core gates passed, but engineering conditions still require disposition.",
        ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL: "Automated evidence and policy gates passed; human engineering approval is still required.",
        ReadinessStatus.APPROVED_FOR_RELEASE: "Automated evidence/policy gates passed and a matching human approval artifact was supplied.",
    }[status]
    metrics = {
        "policy_id": policy.policy_id,
        "policy_sha256": policy.source_sha256,
        "requirements_total": len(requirements),
        "requirements_critical": sum(1 for item in requirements if item.criticality.value == "CRITICAL"),
        "requirements_high": sum(1 for item in requirements if item.criticality.value == "HIGH"),
        "requirements_dynamic_policy": sum(
            1
            for item in requirements
            if item.verification_mode is RequirementVerificationMode.DYNAMIC or item.criticality in policy.require_dynamic_for
        ),
        "requirements_static_policy": sum(
            1
            for item in requirements
            if item.verification_mode is RequirementVerificationMode.STATIC and item.criticality not in policy.require_dynamic_for
        ),
        "requirements_dynamic_verified": sum(1 for item in verifications if item.status is RequirementStatus.DYNAMICALLY_VERIFIED),
        "requirements_static_verified": sum(1 for item in verifications if item.status is RequirementStatus.STATICALLY_VERIFIED),
        "requirements_release_gaps": len(release_gaps),
        "baseline_required_requirements": len(baseline_required),
        "tests_total": len(tests),
        "tests_passed": sum(1 for item in executions if item.status is ExecutionStatus.PASS),
        "tests_failed": sum(1 for item in executions if item.status is ExecutionStatus.FAIL),
        "execution_backend_kind": execution_backend_kind,
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
        policy.require_human_approval,
        approval,
    )
