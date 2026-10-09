from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.controls.build import build_controls_spec
from devagent.controls.ignition import ignition_semantic_projection
from devagent.controls.qualification import qualify_controls_build
from devagent.controls.schema import parse_control_system_payload
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
)


def _spec():
    return parse_control_system_payload(
        {
            "schema": "devagent-controls-spec-v2",
            "project_id": "PACK01",
            "controllers": [
                {
                    "id": "PLC1",
                    "vendor": "ROCKWELL",
                    "platform": "CONTROLLOGIX",
                    "network": "PACKAGING_NET",
                }
            ],
            "equipment": [
                {
                    "id": "CONV_101",
                    "type": "CONVEYOR",
                    "standard": "conveyor-v1",
                    "controller": "PLC1",
                    "area": "PACKAGING",
                    "safety_zone": "SZ01",
                    "signals": ["SAFE", "GUARD_OPEN", "DRIVE_FAULT", "RUN_FB"],
                    "commands": {"START": True, "STOP": True, "RESET": True},
                    "status": ["READY", "RUNNING", "FAULTED"],
                    "permissives": ["SAFE"],
                    "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                    "faults": ["DRIVE_FAULT"],
                    "alarms": [
                        {
                            "id": "ALM_DRIVE",
                            "priority": "HIGH",
                            "operator_response": "Inspect drive fault.",
                            "source_signal": "DRIVE_FAULT",
                        }
                    ],
                    "hmi": {"faceplate": "conveyor-v1", "historian": True},
                    "io": [],
                    "requirements": [
                        {
                            "id": "REQ_GUARD",
                            "text": "CONV_101 must not run while GUARD_OPEN is active.",
                            "criticality": "HIGH",
                            "assertion": {
                                "conditions": {
                                    "COMMAND.START": True,
                                    "SIGNAL.GUARD_OPEN": True,
                                },
                                "expect": {"OUTPUT.RUN": False},
                            },
                        }
                    ],
                }
            ],
        }
    )


def _private_and_store(root: Path) -> tuple[Ed25519PrivateKey, Path]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    store = root / "trust-store.json"
    store.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Plant Security Owner",
                "approved_at": "2026-10-08T18:00:00Z",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(signed, sort_keys=True), encoding="utf-8")
    return path


def test_signed_vendor_runtime_and_human_approval_close_external_loop(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build_controls_spec(_spec(), build)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private, trust_store = _private_and_store(evidence)
    manifest = json.loads((build / "generation-manifest.json").read_text(encoding="utf-8"))

    controller_evidence = evidence / "PLC1"
    controller_evidence.mkdir()
    generated = build / "rockwell" / "PLC1.L5X"
    exported = controller_evidence / "studio5000-export.L5X"
    shutil.copyfile(generated, exported)

    _signed_json(
        controller_evidence / "studio5000-import.json",
        private,
        {
            "schema": "devagent-controls-studio5000-import-evidence-v1",
            "status": "PASS",
            "controller_id": "PLC1",
            "controls_ir_sha256": manifest["controls_ir_sha256"],
            "generated_project_sha256": hashlib.sha256(generated.read_bytes()).hexdigest(),
            "exported_project_sha256": hashlib.sha256(exported.read_bytes()).hexdigest(),
            "tool": "Studio 5000",
            "tool_version": "36.00",
            "imported_at": "2026-10-08T18:05:00Z",
        },
    )

    ignition_hashes = {
        relative: digest
        for relative, digest in manifest["artifact_sha256"].items()
        if relative.startswith("ignition/")
    }
    _signed_json(
        evidence / "ignition-gateway-import.json",
        private,
        {
            "schema": "devagent-controls-ignition-gateway-import-evidence-v1",
            "status": "PASS",
            "controls_ir_sha256": manifest["controls_ir_sha256"],
            "artifact_sha256": ignition_hashes,
            "gateway_id": "PACKAGING-GW-TEST",
            "gateway_version": "8.1",
            "imported_at": "2026-10-08T18:06:00Z",
        },
    )
    ignition_payloads = {
        name: json.loads((build / "ignition" / name).read_text(encoding="utf-8"))
        for name in (
            "udts.json",
            "equipment.json",
            "alarms.json",
            "history.json",
            "views.json",
            "navigation.json",
        )
    }
    ignition_projection = ignition_semantic_projection(ignition_payloads)
    ignition_projection_sha = hashlib.sha256(
        json.dumps(
            ignition_projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    _signed_json(
        evidence / "ignition-gateway-export.json",
        private,
        {
            "schema": "devagent-controls-ignition-gateway-export-evidence-v1",
            "status": "PASS",
            "controls_ir_sha256": manifest["controls_ir_sha256"],
            "gateway_id": "PACKAGING-GW-TEST",
            "gateway_version": "8.1",
            "adapter": "test-normalized-gateway-export",
            "adapter_version": "1.0.0",
            "exported_at": "2026-10-08T18:06:30Z",
            "projection": ignition_projection,
            "projection_sha256": ignition_projection_sha,
        },
    )

    requirements = build / "requirements" / "by-controller" / "PLC1.json"
    static = run_production_verification_v5(
        generated,
        requirement_paths=[requirements],
    )
    project_sha = static.engineering.project.metadata.source_sha256
    registry = _signed_json(
        controller_evidence / "backend-registry.json",
        private,
        {
            "schema": "devagent-plc-execution-backend-registry-v1",
            "approved_by": "Controls Platform Owner",
            "approved_at": "2026-10-08T18:07:00Z",
            "backends": [
                {
                    "id": "plant-simulator",
                    "kind": "SIMULATOR",
                    "status": "QUALIFIED",
                    "project_sha256": [project_sha],
                    "qualification_evidence": ["QUAL-SIM-001"],
                }
            ],
        },
    )
    execution = _signed_json(
        controller_evidence / "execution-results.json",
        private,
        {
            "schema": "devagent-plc-execution-results-v1",
            "project_sha256": project_sha,
            "test_plan_sha256": compute_test_plan_sha256(static.engineering.fat_tests),
            "backend_registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
            "backend": "plant-simulator",
            "run_id": "RUN-001",
            "results": [
                {
                    "test_id": test.id,
                    "status": "PASS",
                    "observed": "Expected behavior observed",
                    "timestamp": "2026-10-08T18:10:00Z",
                    "evidence": [f"trace://{test.id}"],
                }
                for test in static.engineering.fat_tests
            ],
        },
    )

    dynamic = run_production_verification_v5(
        generated,
        requirement_paths=[requirements],
        execution_results_path=execution,
        execution_backend_registry_path=registry,
        trust_store_path=trust_store,
    )
    approval_payload = {
        "project_sha256": dynamic.engineering.project.metadata.source_sha256,
        "test_plan_sha256": compute_test_plan_sha256(dynamic.engineering.fat_tests),
        "requirements_sha256": compute_requirements_sha256(dynamic.requirements),
        "backend_registry_sha256": dynamic.execution_backend_registry_sha256,
        "baseline_sha256": dynamic.baseline_sha256,
        "execution_results_sha256": dynamic.execution_results_sha256,
        "release_policy_sha256": dynamic.release_policy_sha256,
        "trust_store_sha256": dynamic.trust_store_sha256,
        "verification_context_sha256": dynamic.verification_context_sha256,
        "decision": "APPROVE",
        "approved_by": "Lead Controls Engineer",
        "approved_at": "2026-10-08T18:30:00Z",
    }
    _signed_json(
        controller_evidence / "approval.json",
        private,
        approval_payload,
    )

    fat_plan_path = build / "tests" / "fat-plan.json"
    fat_plan = json.loads(fat_plan_path.read_text(encoding="utf-8"))
    runtime_bindings = {
        "PLC1": {
            "backend_id": dynamic.execution_backend_id,
            "execution_results_sha256": dynamic.execution_results_sha256,
            "verification_context_sha256": dynamic.verification_context_sha256,
        }
    }
    _signed_json(
        evidence / "controls-fat-results.json",
        private,
        {
            "schema": "devagent-controls-fat-results-v1",
            "status": "PASS",
            "controls_ir_sha256": manifest["controls_ir_sha256"],
            "generation_manifest_sha256": hashlib.sha256(
                (build / "generation-manifest.json").read_bytes()
            ).hexdigest(),
            "fat_plan_sha256": hashlib.sha256(fat_plan_path.read_bytes()).hexdigest(),
            "runtime_bindings": runtime_bindings,
            "run_id": "CONTROLS-FAT-001",
            "executed_by": "Controls FAT Engineer",
            "environment_id": "PACKAGING-HIL-01",
            "executed_at": "2026-10-08T18:20:00Z",
            "results": [
                {
                    "test_id": case["id"],
                    "status": "PASS",
                    "observed": "Expected integrated PLC/HMI behavior observed",
                    "timestamp": "2026-10-08T18:20:00Z",
                    "evidence": [f"trace://controls/{case['id']}"],
                }
                for case in fat_plan["cases"]
            ],
        },
    )

    preapproval = qualify_controls_build(build, evidence)

    assert preapproval["status"] == "READY_FOR_ENGINEERING_APPROVAL"
    assert preapproval["engineering_approval"]["status"] == "REQUIRED"
    assert preapproval["production_release_ready"] is False

    _signed_json(
        evidence / "controls-engineering-approval.json",
        private,
        {
            "schema": "devagent-controls-engineering-approval-v1",
            "project_id": manifest["project_id"],
            "spec_sha256": manifest["spec_sha256"],
            "controls_ir_sha256": manifest["controls_ir_sha256"],
            "approval_context_sha256": preapproval["approval_context_sha256"],
            "decision": "APPROVE",
            "approved_by": "Lead Controls Engineer",
            "approved_at": "2026-10-08T18:40:00Z",
        },
    )

    qualified = qualify_controls_build(build, evidence)

    assert qualified["status"] == "APPROVED_FOR_RELEASE_HANDOFF"
    assert qualified["studio5000"][0]["status"] == "PASS"
    assert qualified["ignition_gateway"]["status"] == "PASS"
    assert qualified["ignition_gateway"]["semantic_gateway_export_reimport"] == "PASS"
    assert qualified["integrated_fat"]["status"] == "PASS"
    assert qualified["integrated_fat"]["tests_total"] > 0
    assert qualified["runtime"][0]["readiness"] == "APPROVED_FOR_RELEASE"
    assert qualified["engineering_approval"]["status"] == "APPROVED"
    assert qualified["production_release_ready"] is True
    assert qualified["production_deployment_performed"] is False
    assert qualified["deployment_authority_present"] is False
