from __future__ import annotations

from pathlib import Path

from devagent.plc.production import (
    _append_domain_evidence,
    run_production_verification as run_v4_verification,
)
from devagent.plc.production_evidence import evidence_index
from devagent.plc.production_models import (
    EvidenceItem,
    ExecutionStatus,
    ReadinessStatus,
    StageRecord,
    StageStatus,
)
from devagent.plc.production_readiness_v5 import evaluate_release_readiness_v5
from devagent.plc.production_review import (
    detect_risks,
    optimization_candidates,
    recommendations,
)
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
    compute_verification_context_sha256,
    promote_requirement_execution,
)
from devagent.plc.release_policy import load_release_policy
from devagent.plc.signature_trust import load_trusted_signer_store
from devagent.plc.trusted_snapshot import (
    parse_approval_snapshot,
    parse_backend_registry_snapshot,
    parse_execution_results_snapshot,
    parse_release_policy_snapshot,
    read_json_snapshot,
    verify_snapshot_signature,
)
from devagent.providers import ModelProvider

# Production V5 has a non-downgradable authenticity floor. A release policy may
# require additional signature purposes in future revisions, but it cannot make
# these three trust-critical artifacts unsigned.
_MANDATORY_SIGNED_PURPOSES = {
    "EXECUTION_BACKEND_REGISTRY",
    "EXECUTION_RESULTS",
    "HUMAN_APPROVAL",
}


def _append_v5_trust_evidence(result) -> None:
    if result.release_policy is not None and result.release_policy_sha256:
        result.evidence.append(
            EvidenceItem(
                f"RELEASE-POLICY:{result.release_policy_sha256}",
                "RELEASE_POLICY",
                f"Release policy {result.release_policy.get('policy_id')} bound to this verification context.",
                str(result.release_policy.get("source_path") or ""),
                result.release_policy_sha256,
                {
                    "builtin": bool(result.release_policy.get("builtin")),
                    "require_dynamic_for": result.release_policy.get("require_dynamic_for", []),
                    "require_baseline_for": result.release_policy.get("require_baseline_for", []),
                    "allowed_backend_kinds": result.release_policy.get("allowed_backend_kinds", []),
                    "require_signatures_for": result.release_policy.get("require_signatures_for", []),
                    "mandatory_signed_purposes": sorted(_MANDATORY_SIGNED_PURPOSES),
                },
            )
        )
    if result.trust_store is not None and result.trust_store_sha256:
        result.evidence.append(
            EvidenceItem(
                f"TRUST-STORE:{result.trust_store_sha256}",
                "TRUST_STORE",
                f"Operator-supplied signer trust store with {len(result.trust_store.get('signers', []))} signer(s).",
                str(result.trust_store.get("source_path") or ""),
                result.trust_store_sha256,
                {
                    "approved_by": result.trust_store.get("approved_by"),
                    "approved_at": result.trust_store.get("approved_at"),
                },
            )
        )
    for record in result.verified_signatures:
        result.evidence.append(
            EvidenceItem(
                f"SIGNATURE:{record['purpose']}:{record['artifact_sha256']}",
                "VERIFIED_SIGNATURE",
                f"Verified {record['purpose']} Ed25519 signature from trusted key {record['key_id']} over the exact bytes evaluated by V5.",
                record.get("source_path"),
                record.get("artifact_sha256"),
                {
                    "algorithm": record["algorithm"],
                    "key_id": record["key_id"],
                    "trust_store_sha256": record["trust_store_sha256"],
                    "read_once_authenticated_snapshot": True,
                },
            )
        )


def _update_execution_and_review_stages(result) -> None:
    tests = result.engineering.fat_tests
    if not result.executions:
        exec_status = StageStatus.NOT_RUN
        exec_summary = "No authenticated qualified-backend execution evidence supplied; no FAT PASS claims were made."
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
            f"Imported {len(result.executions)} authenticated qualified-backend result(s) from "
            f"{result.execution_backend_id}: {passed} PASS, {failed} FAIL; registry "
            f"{result.execution_backend_registry_sha256[:12]}…."
        )
    result.stages[8] = StageRecord(9, result.stages[8].name, exec_status, exec_summary)

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
    result.stages[9] = StageRecord(
        10,
        result.stages[9].name,
        risk_stage,
        f"Detected {len(result.risks)} evidence-backed risk/review item(s); "
        f"{deterministic_critical} deterministic CRITICAL and "
        f"{deterministic_high_or_medium} deterministic HIGH/MEDIUM.",
    )
    result.stages[10] = StageRecord(
        11,
        result.stages[10].name,
        StageStatus.PASS,
        f"Produced {len(result.optimizations)} bounded optimization candidate(s); no PLC code was modified.",
    )
    result.stages[12] = StageRecord(
        13,
        result.stages[12].name,
        StageStatus.PASS,
        f"Produced {len(result.recommendations)} actionable recommendation(s) linked to evidence/risk IDs.",
    )


def run_production_verification_v5(
    project_path: Path,
    *,
    requirement_paths: list[Path] | tuple[Path, ...] = (),
    baseline_path: Path | None = None,
    execution_results_path: Path | None = None,
    execution_backend_registry_path: Path | None = None,
    approval_path: Path | None = None,
    release_policy_path: Path | None = None,
    trust_store_path: Path | None = None,
    provider: ModelProvider | None = None,
    ai_enabled: bool = False,
    require_ai: bool = False,
    ai_provider_name: str | None = None,
    ai_model_name: str | None = None,
):
    # The trust store is the operator-supplied root of trust and is parsed once
    # into an immutable value object. Every signed policy/evidence artifact is
    # then read exactly once from its source path. Signature verification and
    # deterministic evaluation both consume that same in-memory snapshot.
    trust_store = load_trusted_signer_store(trust_store_path)
    verified_signatures: list[dict] = []

    policy_snapshot = (
        read_json_snapshot(
            release_policy_path,
            max_bytes=1024 * 1024,
            purpose="RELEASE_POLICY",
        )
        if release_policy_path is not None
        else None
    )
    if policy_snapshot is None:
        policy = load_release_policy(None)
    else:
        record = verify_snapshot_signature(
            policy_snapshot,
            purpose="RELEASE_POLICY",
            trust_store=trust_store,
            required=True,
        )
        assert record is not None
        verified_signatures.append(record)
        policy = parse_release_policy_snapshot(policy_snapshot)

    required_purposes = tuple(
        sorted(set(policy.require_signatures_for) | _MANDATORY_SIGNED_PURPOSES)
    )

    registry_snapshot = (
        read_json_snapshot(
            execution_backend_registry_path,
            max_bytes=1024 * 1024,
            purpose="EXECUTION_BACKEND_REGISTRY",
        )
        if execution_backend_registry_path is not None
        else None
    )
    execution_snapshot = (
        read_json_snapshot(
            execution_results_path,
            max_bytes=25 * 1024 * 1024,
            purpose="EXECUTION_RESULTS",
        )
        if execution_results_path is not None
        else None
    )
    approval_snapshot = (
        read_json_snapshot(
            approval_path,
            max_bytes=1024 * 1024,
            purpose="HUMAN_APPROVAL",
        )
        if approval_path is not None
        else None
    )

    for snapshot, purpose in (
        (registry_snapshot, "EXECUTION_BACKEND_REGISTRY"),
        (execution_snapshot, "EXECUTION_RESULTS"),
        (approval_snapshot, "HUMAN_APPROVAL"),
    ):
        if snapshot is None:
            continue
        record = verify_snapshot_signature(
            snapshot,
            purpose=purpose,
            trust_store=trust_store,
            required=purpose in required_purposes,
        )
        if record is not None:
            verified_signatures.append(record)

    if execution_snapshot is not None and registry_snapshot is None:
        raise ValueError(
            "Execution evidence requires --execution-backend-registry with a signed QUALIFIED backend artifact"
        )

    # V4 remains the mature deterministic static/requirements/regression core.
    # Signed execution artifacts are deliberately NOT passed as file paths, so
    # V4 cannot reopen mutable source files after authentication. V5 imports
    # execution facts from the authenticated snapshots below and then recomputes
    # all execution-dependent engineering outputs.
    result = run_v4_verification(
        project_path,
        requirement_paths=requirement_paths,
        baseline_path=baseline_path,
        execution_results_path=None,
        execution_backend_registry_path=None,
        approval_path=None,
        provider=provider,
        ai_enabled=ai_enabled,
        require_ai=require_ai,
        ai_provider_name=ai_provider_name,
        ai_model_name=ai_model_name,
    )

    result.release_policy = policy.jsonable()
    result.release_policy_sha256 = policy.source_sha256
    if trust_store is not None:
        result.trust_store = trust_store.jsonable()
        result.trust_store_sha256 = trust_store.source_sha256
    result.verified_signatures = verified_signatures

    backend_registry = (
        parse_backend_registry_snapshot(registry_snapshot)
        if registry_snapshot is not None
        else None
    )
    if backend_registry is not None:
        result.execution_backend_registry = backend_registry.jsonable()
        result.execution_backend_registry_sha256 = backend_registry.source_sha256

    project_sha = result.engineering.project.metadata.source_sha256
    plan_sha = compute_test_plan_sha256(result.engineering.fat_tests)
    requirements_sha = compute_requirements_sha256(result.requirements)

    result.execution_results_sha256 = execution_snapshot.sha256 if execution_snapshot else None
    result.executions = (
        parse_execution_results_snapshot(
            execution_snapshot,
            project_sha256=project_sha,
            test_plan_sha256=plan_sha,
            test_ids={test.id for test in result.engineering.fat_tests},
            backend_registry=backend_registry,
        )
        if execution_snapshot is not None
        else []
    )
    result.execution_backend_id = result.executions[0].backend if result.executions else None
    if result.execution_backend_id and backend_registry is not None:
        match = next(
            (item for item in backend_registry.backends if item.id == result.execution_backend_id),
            None,
        )
        result.execution_backend_kind = match.kind if match else None

    result.requirement_verification = promote_requirement_execution(
        result.requirement_verification,
        result.executions,
    )
    result.risks = detect_risks(
        result.engineering,
        result.requirement_verification,
        result.executions,
        result.engineering_findings,
    )
    result.optimizations = optimization_candidates(result.engineering, result.risks)
    result.recommendations = recommendations(
        result.risks,
        result.optimizations,
        result.executions,
        result.regression_changes,
    )
    _update_execution_and_review_stages(result)

    # Rebuild domain evidence after execution-dependent facts have been
    # recomputed. This avoids carrying stale V4 risk/recommendation evidence.
    result.evidence = evidence_index(result.engineering)
    _append_domain_evidence(result)

    result.verification_context_sha256 = compute_verification_context_sha256(
        project_sha256=project_sha,
        test_plan_sha256=plan_sha,
        requirements_sha256=requirements_sha,
        backend_registry_sha256=result.execution_backend_registry_sha256,
        baseline_sha256=result.baseline_sha256,
        execution_results_sha256=result.execution_results_sha256,
        release_policy_sha256=result.release_policy_sha256,
        trust_store_sha256=result.trust_store_sha256,
    )

    approval = None
    if approval_snapshot is not None:
        approval = parse_approval_snapshot(
            approval_snapshot,
            expected={
                "project_sha256": project_sha,
                "test_plan_sha256": plan_sha,
                "requirements_sha256": requirements_sha,
                "backend_registry_sha256": result.execution_backend_registry_sha256,
                "baseline_sha256": result.baseline_sha256,
                "execution_results_sha256": result.execution_results_sha256,
                "release_policy_sha256": result.release_policy_sha256,
                "trust_store_sha256": result.trust_store_sha256,
                "verification_context_sha256": result.verification_context_sha256,
            },
        )

    result.readiness = evaluate_release_readiness_v5(
        result.engineering,
        result.requirements,
        result.requirement_verification,
        result.engineering.fat_tests,
        result.executions,
        result.risks,
        result.regression_changes,
        approval,
        policy=policy,
        baseline_sha256=result.baseline_sha256,
        execution_backend_kind=result.execution_backend_kind,
    )

    _append_v5_trust_evidence(result)
    result.stages[13] = StageRecord(
        14,
        result.stages[13].name,
        StageStatus.PASS,
        f"Assembled {len(result.evidence)} evidence item(s), including read-once V5 policy/trust provenance, for auditable FAT/report output.",
        result.stages[13].evidence_ids,
    )
    readiness_stage = (
        StageStatus.BLOCKED
        if result.readiness.status is ReadinessStatus.BLOCKED
        else StageStatus.PASS
        if result.readiness.status
        in {ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL, ReadinessStatus.APPROVED_FOR_RELEASE}
        else StageStatus.PARTIAL
    )
    result.stages[14] = StageRecord(
        15,
        result.stages[14].name,
        readiness_stage,
        f"{result.readiness.status.value} — score {result.readiness.score}/100 under policy {policy.policy_id}. Context {result.verification_context_sha256[:12]}….",
        result.stages[14].evidence_ids,
    )
    return result


__all__ = ["run_production_verification_v5"]
