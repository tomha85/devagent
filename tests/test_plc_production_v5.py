from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.production_models import (
    PLCRequirement,
    ReadinessStatus,
    RequirementCriticality,
    RequirementStatus,
    RequirementVerificationMode,
)
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
)


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ProdV5" TargetType="Controller">
  <Controller Use="Target" Name="ProdV5" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" /></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _private_and_store(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    store = tmp_path / "trust-store.json"
    store.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Plant Security Owner",
                "approved_at": "2026-08-26T13:00:00Z",
                "signers": [
                    {
                        "id": "plant-controls-root",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public).decode("ascii"),
                        "purposes": ["*"],
                        "status": "TRUSTED",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return private, store


def _signed_json(path: Path, private: Ed25519PrivateKey, payload: dict) -> Path:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    signed = dict(payload)
    signed["signature"] = {
        "algorithm": "ED25519",
        "key_id": "plant-controls-root",
        "value_base64": base64.b64encode(private.sign(canonical)).decode("ascii"),
    }
    path.write_text(json.dumps(signed, sort_keys=True), encoding="utf-8")
    return path


def _project_and_requirements(tmp_path: Path, *, criticality: str = "HIGH") -> tuple[Path, Path]:
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    requirements = tmp_path / "requirements.json"
    requirements.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-1",
                        "text": "When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
                        "verification_mode": "STATIC",
                        "criticality": criticality,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return project, requirements


def _policy_payload(*, require_baseline_for: list[str] | None = None) -> dict:
    return {
        "schema": "devagent-plc-release-policy-v1",
        "policy_id": "plant-production-v5",
        "approved_by": "Controls Engineering Manager",
        "approved_at": "2026-08-26T13:00:00Z",
        "require_baseline_for": require_baseline_for or [],
        "require_dynamic_for": ["CRITICAL", "HIGH"],
        "allowed_backend_kinds": ["SIMULATOR"],
        "max_deterministic_risks": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 10},
        "require_all_generated_tests_pass": True,
        "require_human_approval": True,
        "require_signatures_for": [
            "EXECUTION_BACKEND_REGISTRY",
            "EXECUTION_RESULTS",
            "HUMAN_APPROVAL",
        ],
    }


def _signed_policy(tmp_path: Path, private: Ed25519PrivateKey, **kwargs) -> Path:
    return _signed_json(tmp_path / "release-policy.json", private, _policy_payload(**kwargs))


def _signed_registry(tmp_path: Path, private: Ed25519PrivateKey, project_sha: str) -> Path:
    return _signed_json(
        tmp_path / "backend-registry.json",
        private,
        {
            "schema": "devagent-plc-execution-backend-registry-v1",
            "approved_by": "Controls Platform Owner",
            "approved_at": "2026-08-26T13:05:00Z",
            "backends": [
                {
                    "id": "plant-simulator",
                    "kind": "SIMULATOR",
                    "status": "QUALIFIED",
                    "project_sha256": [project_sha],
                    "qualification_evidence": ["QUAL-SIM-2026-001"],
                }
            ],
        },
    )


def _signed_execution(
    tmp_path: Path,
    private: Ed25519PrivateKey,
    static_result,
    registry: Path,
    *,
    run_id: str = "RUN-001",
    name: str = "execution.json",
) -> Path:
    return _signed_json(
        tmp_path / name,
        private,
        {
            "schema": "devagent-plc-execution-results-v1",
            "project_sha256": static_result.engineering.project.metadata.source_sha256,
            "test_plan_sha256": compute_test_plan_sha256(static_result.engineering.fat_tests),
            "backend_registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
            "backend": "plant-simulator",
            "run_id": run_id,
            "results": [
                {
                    "test_id": test.id,
                    "status": "PASS",
                    "observed": "Expected behavior observed",
                    "timestamp": "2026-08-26T13:10:00Z",
                    "evidence": [f"trace://{test.id}"],
                }
                for test in static_result.engineering.fat_tests
            ],
        },
    )


def _signed_approval(tmp_path: Path, private: Ed25519PrivateKey, dynamic_result, *, name: str = "approval.json") -> Path:
    payload = {
        "project_sha256": dynamic_result.engineering.project.metadata.source_sha256,
        "test_plan_sha256": compute_test_plan_sha256(dynamic_result.engineering.fat_tests),
        "requirements_sha256": compute_requirements_sha256(dynamic_result.requirements),
        "backend_registry_sha256": dynamic_result.execution_backend_registry_sha256,
        "baseline_sha256": dynamic_result.baseline_sha256,
        "execution_results_sha256": dynamic_result.execution_results_sha256,
        "release_policy_sha256": dynamic_result.release_policy_sha256,
        "trust_store_sha256": dynamic_result.trust_store_sha256,
        "verification_context_sha256": dynamic_result.verification_context_sha256,
        "decision": "APPROVE",
        "approved_by": "Lead Controls Engineer",
        "approved_at": "2026-08-26T13:30:00Z",
    }
    return _signed_json(tmp_path / name, private, payload)


def test_v5_high_requirement_forces_dynamic_proof_then_signed_human_approval(tmp_path: Path) -> None:
    private, trust_store = _private_and_store(tmp_path)
    project, requirements = _project_and_requirements(tmp_path, criticality="HIGH")
    policy = _signed_policy(tmp_path, private)

    static = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        release_policy_path=policy,
        trust_store_path=trust_store,
    )
    assert static.requirements[0].verification_mode is RequirementVerificationMode.STATIC
    assert static.requirements[0].criticality is RequirementCriticality.HIGH
    assert static.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    assert static.readiness is not None
    assert static.readiness.status is ReadinessStatus.NOT_READY
    assert static.readiness.metrics["requirements_release_gaps"] == 1
    assert len(static.verified_signatures) == 1

    registry = _signed_registry(tmp_path, private, static.engineering.project.metadata.source_sha256)
    execution = _signed_execution(tmp_path, private, static, registry)
    dynamic = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        execution_backend_registry_path=registry,
        execution_results_path=execution,
        release_policy_path=policy,
        trust_store_path=trust_store,
    )
    assert dynamic.requirement_verification[0].status is RequirementStatus.DYNAMICALLY_VERIFIED
    assert dynamic.execution_backend_kind == "SIMULATOR"
    assert dynamic.execution_results_sha256 == hashlib.sha256(execution.read_bytes()).hexdigest()
    assert dynamic.readiness is not None
    assert dynamic.readiness.status is ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL
    assert len(dynamic.verified_signatures) == 3

    approval = _signed_approval(tmp_path, private, dynamic)
    approved = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        execution_backend_registry_path=registry,
        execution_results_path=execution,
        release_policy_path=policy,
        trust_store_path=trust_store,
        approval_path=approval,
    )
    assert approved.readiness is not None
    assert approved.readiness.status is ReadinessStatus.APPROVED_FOR_RELEASE
    assert len(approved.verified_signatures) == 4
    assert approved.readiness.human_approval is not None


def test_v5_approval_is_invalid_after_execution_artifact_changes(tmp_path: Path) -> None:
    private, trust_store = _private_and_store(tmp_path)
    project, requirements = _project_and_requirements(tmp_path)
    policy = _signed_policy(tmp_path, private)
    static = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        release_policy_path=policy,
        trust_store_path=trust_store,
    )
    registry = _signed_registry(tmp_path, private, static.engineering.project.metadata.source_sha256)
    execution = _signed_execution(tmp_path, private, static, registry)
    dynamic = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        execution_backend_registry_path=registry,
        execution_results_path=execution,
        release_policy_path=policy,
        trust_store_path=trust_store,
    )
    approval = _signed_approval(tmp_path, private, dynamic)
    changed_execution = _signed_execution(
        tmp_path,
        private,
        static,
        registry,
        run_id="RUN-002",
        name="execution-changed.json",
    )

    with pytest.raises(ValueError, match="Approval execution_results_sha256 does not match"):
        run_production_verification_v5(
            project,
            requirement_paths=[requirements],
            execution_backend_registry_path=registry,
            execution_results_path=changed_execution,
            release_policy_path=policy,
            trust_store_path=trust_store,
            approval_path=approval,
        )


def test_v5_critical_requirement_can_require_regression_baseline(tmp_path: Path) -> None:
    private, trust_store = _private_and_store(tmp_path)
    project, requirements = _project_and_requirements(tmp_path, criticality="CRITICAL")
    policy = _signed_policy(tmp_path, private, require_baseline_for=["CRITICAL"])
    static = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        release_policy_path=policy,
        trust_store_path=trust_store,
    )
    registry = _signed_registry(tmp_path, private, static.engineering.project.metadata.source_sha256)
    execution = _signed_execution(tmp_path, private, static, registry)
    result = run_production_verification_v5(
        project,
        requirement_paths=[requirements],
        execution_backend_registry_path=registry,
        execution_results_path=execution,
        release_policy_path=policy,
        trust_store_path=trust_store,
    )

    assert result.readiness is not None
    assert result.readiness.status is ReadinessStatus.NOT_READY
    assert any("requires a regression baseline" in item for item in result.readiness.blockers)


def test_v5_external_release_policy_is_always_signed(tmp_path: Path) -> None:
    _, trust_store = _private_and_store(tmp_path)
    project, requirements = _project_and_requirements(tmp_path)
    unsigned_policy = tmp_path / "unsigned-policy.json"
    unsigned_policy.write_text(json.dumps(_policy_payload()), encoding="utf-8")

    with pytest.raises(ValueError, match="requires an Ed25519 signature"):
        run_production_verification_v5(
            project,
            requirement_paths=[requirements],
            release_policy_path=unsigned_policy,
            trust_store_path=trust_store,
        )


def test_requirement_hash_changes_when_criticality_changes() -> None:
    base = dict(
        id="REQ-1",
        text="Run shall be TRUE.",
        source_path="requirements.json",
        source_locator="item 1",
        source_sha256="a" * 64,
        verification_mode=RequirementVerificationMode.STATIC,
    )
    high = PLCRequirement(**base, criticality=RequirementCriticality.HIGH)
    critical = PLCRequirement(**base, criticality=RequirementCriticality.CRITICAL)

    assert compute_requirements_sha256([high]) != compute_requirements_sha256([critical])
