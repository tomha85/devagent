from __future__ import annotations

from pathlib import Path
from typing import Iterable

from devagent.plc.execution_trust import load_execution_backend_registry
from devagent.plc.models import PLCOutcome
from devagent.plc.production_ai import run_ai_requirement_mapping, run_ai_review
from devagent.plc.production_evidence import deterministic_engineering_findings, evidence_index
from devagent.plc.production_models import (
    EvidenceItem,
    ExecutionStatus,
    PLCProductionResult,
    ReadinessStatus,
    RequirementStatus,
    StageRecord,
    StageStatus,
)
from devagent.plc.production_readiness import evaluate_release_readiness, load_approval
from devagent.plc.production_regression import analyze_regression
from devagent.plc.production_review import detect_risks, optimization_candidates, recommendations
from devagent.plc.production_utils import source_locator
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
    compute_verification_context_sha256,
    generate_requirement_tests,
    link_tests_to_verifications,
    load_execution_results,
    promote_requirement_execution,
    verify_requirement,
)
from devagent.plc.requirements import ingest_requirements
from devagent.plc.safe_analysis import analyze_rockwell_l5x
from devagent.providers import ModelProvider, ProviderError

STAGE_NAMES = (
    "PROJECT VALIDATION",
    "CANONICAL PLC IR",
    "LOGIC SEMANTICS",
    "DEPENDENCY GRAPH",
    "AI ENGINEERING REVIEW",
    "REQUIREMENT INGESTION",
    "REQUIREMENT VERIFICATION",
    "TEST GENERATION",
    "TEST EXECUTION",
    "RISK DETECTION",
    "OPTIMIZATION REVIEW",
    "REGRESSION ANALYSIS",
    "RECOMMENDATIONS",
    "EVIDENCE + FAT REPORT",
    "RELEASE READINESS",
)


def _stage(number: int, status: StageStatus, summary: str, evidence_ids: Iterable[str] = ()) -> StageRecord:
    return StageRecord(number, STAGE_NAMES[number - 1], status, summary, tuple(evidence_ids))


def _append_domain_evidence(result: PLCProductionResult) -> None:
    project_sha = result.engineering.project.metadata.source_sha256
    if result.execution_backend_registry is not None:
        registry = result.execution_backend_registry
        registry_sha = result.execution_backend_registry_sha256
        result.evidence.append(
            EvidenceItem(
                f"BACKEND-REGISTRY:{registry_sha}",
                "EXECUTION_BACKEND_REGISTRY",
                f"Approved execution backend registry by {registry.get('approved_by')} with {len(registry.get('backends', []))} backend(s).",
                str(registry.get("source_path") or ""),
                registry_sha,
                {
                    "approved_at": registry.get("approved_at"),
                    "backend_ids": [item.get("id") for item in registry.get("backends", [])],
                },
            )
        )
    for requirement in result.requirements:
        result.evidence.append(
            EvidenceItem(
                requirement.id,
                "REQUIREMENT",
                requirement.text,
                f"{requirement.source_path}:{requirement.source_locator}",
                requirement.source_sha256,
                {"verification_mode": requirement.verification_mode.value},
            )
        )
    for verification in result.requirement_verification:
        result.evidence.append(
            EvidenceItem(
                f"REQV:{verification.requirement_id}",
                "REQUIREMENT_VERIFICATION",
                f"{verification.status.value}: {verification.summary}",
                payload={
                    "evidence_ids": list(verification.evidence_ids),
                    "linked_test_ids": list(verification.linked_test_ids),
                },
            )
        )
    for test in result.engineering.fat_tests:
        result.evidence.append(
            EvidenceItem(
                test.id,
                "FAT_TEST",
                f"{test.title} — {test.expected}",
                source_locator(test.source),
                project_sha,
                {
                    "preconditions": dict(test.preconditions),
                    "execution_status": test.execution_status,
                    "scenario": test.scenario,
                },
            )
        )
    for execution in result.executions:
        result.evidence.append(
            EvidenceItem(
                f"EXEC:{execution.test_id}",
                "TEST_EXECUTION",
                f"{execution.backend}/{execution.run_id}: {execution.test_id}={execution.status.value}",
                payload={
                    "observed": execution.observed,
                    "timestamp": execution.timestamp,
                    "evidence": list(execution.evidence),
                    "backend_registry_sha256": result.execution_backend_registry_sha256,
                },
            )
        )
    for item in [
        *result.engineering_findings,
        *result.risks,
        *result.optimizations,
        *result.regression_changes,
        *result.recommendations,
    ]:
        result.evidence.append(
            EvidenceItem(
                item.id,
                type(item).__name__.upper(),
                str(getattr(item, "title", getattr(item, "summary", item.id))),
                payload={"evidence_ids": list(getattr(item, "evidence_ids", ()))},
            )
        )


def run_production_verification(
    project_path: Path,
    *,
    requirement_paths: list[Path] | tuple[Path, ...] = (),
    baseline_path: Path | None = None,
    execution_results_path: Path | None = None,
    execution_backend_registry_path: Path | None = None,
    approval_path: Path | None = None,
    provider: ModelProvider | None = None,
    ai_enabled: bool = False,
    require_ai: bool = False,
    ai_provider_name: str | None = None,
    ai_model_name: str | None = None,
) -> PLCProductionResult:
    engineering = analyze_rockwell_l5x(project_path)
    result = PLCProductionResult(
        engineering=engineering,
        ai_provider=ai_provider_name,
        ai_model=ai_model_name,
    )
    result.stages.append(
        _stage(
            1,
            StageStatus.PASS,
            f"Validated Rockwell full-project L5X for {engineering.project.metadata.controller_name}.",
        )
    )
    result.stages.append(
        _stage(
            2,
            StageStatus.PASS,
            f"Canonical IR: {len(engineering.project.tags)} tags, {len(engineering.project.routines)} routines, {len(engineering.project.rungs)} RLL rungs, {engineering.project.st_statement_total} ST statements.",
        )
    )
    result.stages.append(
        _stage(
            3,
            StageStatus.PASS if engineering.outcome is PLCOutcome.STATICALLY_VERIFIED else StageStatus.PARTIAL,
            f"Logic semantics outcome: {engineering.outcome.value}.",
        )
    )
    result.stages.append(
        _stage(
            4,
            StageStatus.PASS,
            f"Dependency graph contains {len(engineering.graph.edges)} evidence-linked edges.",
        )
    )

    result.evidence = evidence_index(engineering)
    result.engineering_findings = deterministic_engineering_findings(
        engineering,
        {item.id for item in result.evidence},
    )
    ai_review_count = 0
    ai_review_ok = False
    if ai_enabled:
        if provider is None:
            message = "AI review requested but no provider was configured"
            if require_ai:
                raise ProviderError(message)
            result.warnings.append(message)
        else:
            try:
                ai_findings, warnings = run_ai_review(
                    provider,
                    engineering,
                    result.evidence,
                    result.engineering_findings,
                )
                result.engineering_findings.extend(ai_findings)
                result.warnings.extend(warnings)
                ai_review_count = len(ai_findings)
                ai_review_ok = True
            except ProviderError as exc:
                if require_ai:
                    raise
                result.warnings.append(f"AI engineering review not completed: {exc}")
    if not ai_enabled:
        ai_stage_status = StageStatus.SKIPPED
        ai_stage_summary = (
            f"AI review disabled; retained {len(result.engineering_findings)} deterministic engineering finding(s)."
        )
    elif ai_review_ok:
        ai_stage_status = StageStatus.PASS
        ai_stage_summary = (
            f"Produced {len(result.engineering_findings)} engineering finding(s), including {ai_review_count} evidence-constrained AI candidate(s)."
        )
    else:
        ai_stage_status = StageStatus.PARTIAL
        ai_stage_summary = "AI review was requested but did not complete; deterministic findings remain authoritative."
    result.stages.append(_stage(5, ai_stage_status, ai_stage_summary))

    result.requirements = ingest_requirements(requirement_paths) if requirement_paths else []
    result.stages.append(
        _stage(
            6,
            StageStatus.PASS if result.requirements else StageStatus.SKIPPED,
            f"Ingested {len(result.requirements)} requirement(s) from {len(requirement_paths)} artifact(s)."
            if result.requirements
            else "No requirement artifact supplied.",
        )
    )

    result.requirement_verification = [
        verify_requirement(requirement, engineering, result.evidence, engineering.fat_tests)
        for requirement in result.requirements
    ]
    if ai_enabled and provider is not None and result.requirements:
        try:
            updates, warnings = run_ai_requirement_mapping(
                provider,
                result.requirements,
                result.requirement_verification,
                result.evidence,
            )
            result.requirement_verification = [
                updates.get(item.requirement_id, item) for item in result.requirement_verification
            ]
            result.warnings.extend(warnings)
        except ProviderError as exc:
            if require_ai:
                raise
            result.warnings.append(f"AI requirement mapping not completed: {exc}")
    if result.requirements:
        proven = sum(
            1
            for item in result.requirement_verification
            if item.status is RequirementStatus.STATICALLY_VERIFIED
        )
        result.stages.append(
            _stage(
                7,
                StageStatus.PASS if proven == len(result.requirements) else StageStatus.PARTIAL,
                f"Statically verified {proven}/{len(result.requirements)} requirement(s); release policy may still require qualified dynamic execution.",
            )
        )
    else:
        result.stages.append(
            _stage(
                7,
                StageStatus.SKIPPED,
                "Requirement verification skipped because no requirements were supplied.",
            )
        )

    tests = generate_requirement_tests(
        result.requirements,
        result.requirement_verification,
        engineering,
    )
    result.requirement_verification = link_tests_to_verifications(
        result.requirement_verification,
        result.requirements,
        tests,
    )
    plan_sha = compute_test_plan_sha256(tests)
    result.stages.append(
        _stage(
            8,
            StageStatus.PASS if tests else StageStatus.PARTIAL,
            f"Generated {len(tests)} evidence-linked FAT candidate(s); plan SHA-256 {plan_sha[:12]}…; all remain NOT_RUN until qualified execution evidence is imported.",
        )
    )

    backend_registry = load_execution_backend_registry(execution_backend_registry_path)
    if backend_registry is not None:
        result.execution_backend_registry = backend_registry.jsonable()
        result.execution_backend_registry_sha256 = backend_registry.source_sha256
    result.executions = load_execution_results(
        execution_results_path,
        engineering.project.metadata.source_sha256,
        plan_sha,
        {test.id for test in tests},
        backend_registry,
    )
    result.execution_backend_id = result.executions[0].backend if result.executions else None
    result.requirement_verification = promote_requirement_execution(
        result.requirement_verification,
        result.executions,
    )
    if not result.executions:
        exec_status = StageStatus.NOT_RUN
        exec_summary = "No qualified execution evidence supplied; no FAT PASS claims were made."
    else:
        passed = sum(1 for item in result.executions if item.status is ExecutionStatus.PASS)
        failed = sum(1 for item in result.executions if item.status is ExecutionStatus.FAIL)
        exec_status = (
            StageStatus.BLOCKED
            if failed
            else StageStatus.PASS
            if passed == len(tests) and len(result.executions) == len(tests)
            else StageStatus.PARTIAL
        )
        exec_summary = (
            f"Imported {len(result.executions)} qualified-backend execution result(s) from {result.execution_backend_id}: {passed} PASS, {failed} FAIL; registry {result.execution_backend_registry_sha256[:12]}…."
        )
    result.stages.append(_stage(9, exec_status, exec_summary))

    result.risks = detect_risks(
        engineering,
        result.requirement_verification,
        result.executions,
        result.engineering_findings,
    )
    deterministic_critical = sum(
        1
        for item in result.risks
        if item.origin == "DETERMINISTIC" and item.severity.value == "CRITICAL"
    )
    deterministic_high_or_medium = sum(
        1
        for item in result.risks
        if item.origin == "DETERMINISTIC" and item.severity.value in {"HIGH", "MEDIUM"}
    )
    risk_stage = (
        StageStatus.BLOCKED
        if deterministic_critical
        else StageStatus.PARTIAL
        if deterministic_high_or_medium
        else StageStatus.PASS
    )
    result.stages.append(
        _stage(
            10,
            risk_stage,
            f"Detected {len(result.risks)} evidence-backed risk/review item(s); {deterministic_critical} deterministic CRITICAL and {deterministic_high_or_medium} deterministic HIGH/MEDIUM.",
        )
    )

    result.optimizations = optimization_candidates(engineering, result.risks)
    result.stages.append(
        _stage(
            11,
            StageStatus.PASS,
            f"Produced {len(result.optimizations)} bounded optimization candidate(s); no PLC code was modified.",
        )
    )

    result.regression_changes, baseline = analyze_regression(
        baseline_path,
        engineering,
        result.requirement_verification,
    )
    result.baseline_sha256 = (
        baseline.project.metadata.source_sha256 if baseline is not None else None
    )
    result.stages.append(
        _stage(
            12,
            StageStatus.PASS if baseline_path is not None else StageStatus.SKIPPED,
            f"Detected {len(result.regression_changes)} semantic/tag regression change(s) against baseline SHA-256 {result.baseline_sha256[:12]}…."
            if baseline_path is not None
            else "No baseline project supplied.",
        )
    )

    result.recommendations = recommendations(
        result.risks,
        result.optimizations,
        result.executions,
        result.regression_changes,
    )
    result.stages.append(
        _stage(
            13,
            StageStatus.PASS,
            f"Produced {len(result.recommendations)} actionable recommendation(s) linked to evidence/risk IDs.",
        )
    )

    _append_domain_evidence(result)
    result.stages.append(
        _stage(
            14,
            StageStatus.PASS,
            f"Assembled {len(result.evidence)} evidence item(s) for auditable FAT/report output.",
        )
    )

    requirements_sha = compute_requirements_sha256(result.requirements)
    result.verification_context_sha256 = compute_verification_context_sha256(
        project_sha256=engineering.project.metadata.source_sha256,
        test_plan_sha256=plan_sha,
        requirements_sha256=requirements_sha,
        backend_registry_sha256=result.execution_backend_registry_sha256,
        baseline_sha256=result.baseline_sha256,
    )
    approval = load_approval(
        approval_path,
        project_sha256=engineering.project.metadata.source_sha256,
        test_plan_sha256=plan_sha,
        requirements_sha256=requirements_sha,
        backend_registry_sha256=result.execution_backend_registry_sha256,
        baseline_sha256=result.baseline_sha256,
        verification_context_sha256=result.verification_context_sha256,
    )
    result.readiness = evaluate_release_readiness(
        engineering,
        result.requirements,
        result.requirement_verification,
        tests,
        result.executions,
        result.risks,
        result.regression_changes,
        approval,
    )
    readiness_stage = (
        StageStatus.BLOCKED
        if result.readiness.status is ReadinessStatus.BLOCKED
        else StageStatus.PASS
        if result.readiness.status
        in {
            ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL,
            ReadinessStatus.APPROVED_FOR_RELEASE,
        }
        else StageStatus.PARTIAL
    )
    result.stages.append(
        _stage(
            15,
            readiness_stage,
            f"{result.readiness.status.value} — score {result.readiness.score}/100. {result.readiness.summary} Context {result.verification_context_sha256[:12]}….",
        )
    )
    return result


__all__ = [
    "compute_requirements_sha256",
    "compute_test_plan_sha256",
    "compute_verification_context_sha256",
    "run_production_verification",
]
