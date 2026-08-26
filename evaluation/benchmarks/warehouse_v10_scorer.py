from __future__ import annotations

from typing import Any

from devagent.plc import run_production_verification_v5
from devagent.plc.production_models import RequirementStatus
from evaluation.benchmarks.warehouse_sortation_v10 import BENCHMARK_NAME, DEFECTS


def _requirement_signal(result, requirement_id: str) -> bool:
    verification = next(
        (item for item in result.requirement_verification if item.requirement_id == requirement_id),
        None,
    )
    return bool(
        verification
        and verification.status
        in {RequirementStatus.TRACEABLE_NOT_PROVEN, RequirementStatus.CONFLICT}
    )


def _risk_signal(result, subject: str) -> bool:
    folded = subject.casefold()
    return any(
        folded in " ".join((risk.title, risk.summary, *risk.evidence_ids)).casefold()
        for risk in result.risks
    )


def _regression_signal(result, subject: str) -> bool:
    folded = subject.casefold()
    return any(
        folded in change.subject.casefold()
        or any(folded in tag.casefold() for tag in change.affected_tags)
        for change in result.regression_changes
    )


def score_warehouse_v10(defective_project, *, baseline_project, requirements_path) -> dict[str, Any]:
    """Score the production agent from hidden ground truth without feeding answers to it."""

    result = run_production_verification_v5(
        defective_project,
        requirement_paths=[requirements_path],
        baseline_path=baseline_project,
    )
    verification_by_id = {item.requirement_id: item for item in result.requirement_verification}
    defect_results: list[dict[str, Any]] = []
    false_verified: list[str] = []
    not_mapped: list[str] = []

    for defect in DEFECTS:
        req_id = defect.get("requirement_id")
        detected = False
        signals: list[str] = []
        if req_id:
            verification = verification_by_id.get(str(req_id))
            if verification is not None and verification.status in {
                RequirementStatus.STATICALLY_VERIFIED,
                RequirementStatus.DYNAMICALLY_VERIFIED,
            }:
                false_verified.append(str(defect["id"]))
            if verification is None or verification.status is RequirementStatus.NOT_MAPPED:
                not_mapped.append(str(defect["id"]))
            if _requirement_signal(result, str(req_id)):
                detected = True
                signals.append("REQUIREMENT_GAP")
        if _risk_signal(result, str(defect["subject"])):
            detected = True
            signals.append("RISK")
        if _regression_signal(result, str(defect["subject"])):
            detected = True
            signals.append("REGRESSION")
        defect_results.append({**defect, "detected": detected, "signals": sorted(set(signals))})

    def recall(items: list[dict[str, Any]]) -> float:
        return 1.0 if not items else sum(1 for item in items if item["detected"]) / len(items)

    critical = [item for item in defect_results if item["severity"] == "CRITICAL"]
    high = [item for item in defect_results if item["severity"] == "HIGH"]
    detected_total = sum(1 for item in defect_results if item["detected"])
    project = result.engineering.project
    plan = result.project_test_plan
    if plan is None:
        raise AssertionError("V10 benchmark requires project-specific test planning")

    return {
        "schema": "devagent-rockwell-warehouse-benchmark-score-v2",
        "benchmark": BENCHMARK_NAME,
        "project_sha256": project.metadata.source_sha256,
        "baseline_sha256": result.baseline_sha256,
        "inventory": {
            "tags": len(project.tags),
            "programs": len(project.programs),
            "routines": len(project.routines),
            "rll_rungs": len(project.rungs),
            "st_statements": project.st_statement_total,
            "aois": len(project.aois),
        },
        "coverage": {
            "instruction": project.instruction_semantic_coverage,
            "branch": project.branch_semantic_coverage,
            "structured_text": project.st_semantic_coverage,
            "aoi_body": (
                1.0
                if project.aoi_internal_total == 0
                else project.aoi_internal_modeled_count / project.aoi_internal_total
            ),
            "aoi_call": (
                1.0
                if project.aoi_call_total == 0
                else project.aoi_call_bound_count / project.aoi_call_total
            ),
        },
        "project_test_plan": {
            "behavior_count": len(plan.behaviors),
            "test_intent_count": len(plan.test_intents),
            "summary": plan.summary,
            "scenarios": sorted({item.scenario for item in plan.test_intents}),
        },
        "defects": defect_results,
        "metrics": {
            "seeded_defects": len(defect_results),
            "detected": detected_total,
            "overall_recall": detected_total / len(defect_results),
            "critical_recall": recall(critical),
            "high_recall": recall(high),
            "false_verified_defects": sorted(set(false_verified)),
            "not_mapped_defects": sorted(set(not_mapped)),
            "requirements_total": len(result.requirements),
            "fat_tests": len(result.engineering.fat_tests),
            "test_intents": len(plan.test_intents),
            "regression_changes": len(result.regression_changes),
            "risks": len(result.risks),
            "readiness": result.readiness.status.value if result.readiness else None,
        },
    }


__all__ = ["score_warehouse_v10"]
