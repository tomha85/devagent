from __future__ import annotations

import hashlib
from pathlib import Path

from devagent.plc.production import run_production_verification as run_v4_verification
from devagent.plc.production_models import EvidenceItem, ReadinessStatus, StageRecord, StageStatus
from devagent.plc.production_readiness_v5 import (
    evaluate_release_readiness_v5,
    load_approval_v5,
)
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
    compute_verification_context_sha256,
)
from devagent.plc.release_policy import load_release_policy
from devagent.plc.signature_trust import (
    load_trusted_signer_store,
    verify_signed_json_artifact,
)
from devagent.providers import ModelProvider


def _sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    return hashlib.sha256(path.expanduser().resolve(strict=True).read_bytes()).hexdigest()


def _verify_optional_artifact(
    path: Path | None,
    *,
    purpose: str,
    required_purposes: tuple[str, ...],
    trust_store,
) -> dict | None:
    if path is None:
        return None
    return verify_signed_json_artifact(
        path,
        purpose=purpose,
        trust_store=trust_store,
        required=purpose in required_purposes,
    )


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
                f"Verified {record['purpose']} Ed25519 signature from trusted key {record['key_id']}.",
                record.get("source_path"),
                record.get("artifact_sha256"),
                {
                    "algorithm": record["algorithm"],
                    "key_id": record["key_id"],
                    "trust_store_sha256": record["trust_store_sha256"],
                },
            )
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
    policy = load_release_policy(release_policy_path)
    trust_store = load_trusted_signer_store(trust_store_path)
    verified_signatures: list[dict] = []

    # An external release policy defines the production gates themselves, so it
    # is always authenticated by root trust. The policy cannot opt out of
    # authenticating itself by removing a field from its own content.
    if release_policy_path is not None:
        record = verify_signed_json_artifact(
            release_policy_path,
            purpose="RELEASE_POLICY",
            trust_store=trust_store,
            required=True,
        )
        assert record is not None
        verified_signatures.append(record)

    required_purposes = policy.require_signatures_for
    for path, purpose in (
        (execution_backend_registry_path, "EXECUTION_BACKEND_REGISTRY"),
        (execution_results_path, "EXECUTION_RESULTS"),
    ):
        record = _verify_optional_artifact(
            path,
            purpose=purpose,
            required_purposes=required_purposes,
            trust_store=trust_store,
        )
        if record is not None:
            verified_signatures.append(record)

    # Run the mature V4 semantic/requirements/FAT/risk/regression pipeline with
    # approval intentionally withheld. V5 adds its stronger release policy,
    # signature, and exact-evidence-context gate after all deterministic facts
    # have been assembled.
    result = run_v4_verification(
        project_path,
        requirement_paths=requirement_paths,
        baseline_path=baseline_path,
        execution_results_path=execution_results_path,
        execution_backend_registry_path=execution_backend_registry_path,
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
    result.execution_results_sha256 = _sha256(execution_results_path)

    if result.execution_backend_id and result.execution_backend_registry:
        match = next(
            (
                item
                for item in result.execution_backend_registry.get("backends", [])
                if item.get("id") == result.execution_backend_id
            ),
            None,
        )
        result.execution_backend_kind = str(match.get("kind")) if match else None

    project_sha = result.engineering.project.metadata.source_sha256
    plan_sha = compute_test_plan_sha256(result.engineering.fat_tests)
    requirements_sha = compute_requirements_sha256(result.requirements)
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

    approval_signature = _verify_optional_artifact(
        approval_path,
        purpose="HUMAN_APPROVAL",
        required_purposes=required_purposes,
        trust_store=trust_store,
    )
    if approval_signature is not None:
        result.verified_signatures.append(approval_signature)

    approval = load_approval_v5(
        approval_path,
        project_sha256=project_sha,
        test_plan_sha256=plan_sha,
        requirements_sha256=requirements_sha,
        backend_registry_sha256=result.execution_backend_registry_sha256,
        baseline_sha256=result.baseline_sha256,
        execution_results_sha256=result.execution_results_sha256,
        release_policy_sha256=result.release_policy_sha256,
        trust_store_sha256=result.trust_store_sha256,
        verification_context_sha256=result.verification_context_sha256,
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
        f"Assembled {len(result.evidence)} evidence item(s), including V5 policy/trust provenance, for auditable FAT/report output.",
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
