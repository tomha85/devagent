from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from devagent.controls.build import ControlsBuildError, verify_controls_build
from devagent.controls.ir import build_controls_ir, controls_ir_sha256
from devagent.controls.schema import parse_control_system_payload
from devagent.controls.symbols import controller_symbol
from devagent.controls.verification import verify_rockwell_roundtrip
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.signature_trust import load_trusted_signer_store
from devagent.plc.trusted_snapshot import (
    read_json_snapshot,
    verify_snapshot_signature,
)


class ControlsQualificationError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlsQualificationError(f"invalid qualification JSON {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ControlsQualificationError(f"qualification artifact must be a JSON object: {path}")
    return loaded


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ControlsQualificationError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlsQualificationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ControlsQualificationError(f"{field} must include a timezone")
    return text


def _signed_snapshot(
    path: Path,
    *,
    purpose: str,
    trust_store,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = read_json_snapshot(path, max_bytes=max_bytes, purpose=purpose)
    signature = verify_snapshot_signature(
        snapshot,
        purpose=purpose,
        trust_store=trust_store,
        required=True,
    )
    assert signature is not None
    return snapshot.data, signature


def _studio5000_qualification(
    *,
    build: Path,
    evidence_root: Path,
    controller,
    ir,
    manifest: dict[str, Any],
    trust_store,
) -> dict[str, Any]:
    symbol = controller_symbol(controller.id)
    controller_evidence = evidence_root / symbol
    metadata_path = controller_evidence / "studio5000-import.json"
    export_path = controller_evidence / "studio5000-export.L5X"
    if not metadata_path.is_file() or not export_path.is_file():
        raise ControlsQualificationError(
            f"{controller.id} requires signed studio5000-import.json and "
            f"studio5000-export.L5X under {controller_evidence}"
        )

    metadata, signature = _signed_snapshot(
        metadata_path,
        purpose="CONTROLS_STUDIO5000_IMPORT",
        trust_store=trust_store,
    )
    if metadata.get("schema") != "devagent-controls-studio5000-import-evidence-v1":
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence has unsupported schema"
        )
    if metadata.get("status") != "PASS":
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence status must be PASS"
        )
    if metadata.get("controller_id") != controller.id:
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence controller_id mismatch"
        )
    if metadata.get("controls_ir_sha256") != controls_ir_sha256(ir):
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence Controls IR hash mismatch"
        )

    output = next(
        (
            row for row in manifest["rockwell_outputs"]
            if row["controller_id"] == controller.id
        ),
        None,
    )
    if output is None:
        raise ControlsQualificationError(
            f"{controller.id} is missing from generation manifest Rockwell outputs"
        )
    generated_path = build / output["path"]
    generated_sha = _sha256(generated_path)
    exported_sha = _sha256(export_path)
    if metadata.get("generated_project_sha256") != generated_sha:
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence generated project hash mismatch"
        )
    if metadata.get("exported_project_sha256") != exported_sha:
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence exported project hash mismatch"
        )
    tool = str(metadata.get("tool") or "").strip()
    version = str(metadata.get("tool_version") or "").strip()
    if tool.casefold() != "studio 5000" or not version:
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 evidence requires tool='Studio 5000' "
            "and non-empty tool_version"
        )
    imported_at = _timestamp(
        metadata.get("imported_at"),
        field=f"{controller.id} Studio 5000 imported_at",
    )

    semantic = verify_rockwell_roundtrip(ir, controller, export_path)
    if semantic["status"] != "PASS":
        raise ControlsQualificationError(
            f"{controller.id} Studio 5000 re-export semantic comparison failed: "
            + " | ".join(semantic["mismatches"])
        )
    return {
        "status": "PASS",
        "controller_id": controller.id,
        "tool": tool,
        "tool_version": version,
        "imported_at": imported_at,
        "generated_project_sha256": generated_sha,
        "exported_project_sha256": exported_sha,
        "semantic_projection_status": semantic["semantic_projection_status"],
        "expected_semantic_sha256": semantic["expected_semantic_sha256"],
        "actual_semantic_sha256": semantic["actual_semantic_sha256"],
        "signature": signature,
    }


def _ignition_gateway_qualification(
    *,
    build: Path,
    evidence_root: Path,
    ir,
    manifest: dict[str, Any],
    trust_store,
) -> dict[str, Any]:
    evidence_path = evidence_root / "ignition-gateway-import.json"
    if not evidence_path.is_file():
        raise ControlsQualificationError(
            f"Ignition qualification requires signed {evidence_path.name}"
        )
    metadata, signature = _signed_snapshot(
        evidence_path,
        purpose="CONTROLS_IGNITION_GATEWAY_IMPORT",
        trust_store=trust_store,
    )
    if metadata.get("schema") != "devagent-controls-ignition-gateway-import-evidence-v1":
        raise ControlsQualificationError("Ignition Gateway evidence has unsupported schema")
    if metadata.get("status") != "PASS":
        raise ControlsQualificationError("Ignition Gateway evidence status must be PASS")
    if metadata.get("controls_ir_sha256") != controls_ir_sha256(ir):
        raise ControlsQualificationError("Ignition Gateway evidence Controls IR hash mismatch")

    expected_hashes = {
        relative: digest
        for relative, digest in manifest["artifact_sha256"].items()
        if relative.startswith("ignition/")
    }
    supplied = metadata.get("artifact_sha256")
    if supplied != expected_hashes:
        raise ControlsQualificationError(
            "Ignition Gateway evidence artifact_sha256 does not exactly match "
            "the verified staging artifacts"
        )
    gateway_version = str(metadata.get("gateway_version") or "").strip()
    if not gateway_version:
        raise ControlsQualificationError(
            "Ignition Gateway evidence requires gateway_version"
        )
    imported_at = _timestamp(
        metadata.get("imported_at"),
        field="Ignition Gateway imported_at",
    )
    return {
        "status": "PASS",
        "gateway_version": gateway_version,
        "imported_at": imported_at,
        "artifact_sha256": expected_hashes,
        "evidence_mode": "SIGNED_IMPORT_EVIDENCE",
        "semantic_gateway_export_reimport": "NOT_IMPLEMENTED",
        "signature": signature,
    }


def qualify_controls_build(
    build_dir: Path,
    evidence_dir: Path,
) -> dict[str, Any]:
    build = build_dir.expanduser().resolve(strict=True)
    evidence_root = evidence_dir.expanduser().resolve(strict=True)

    verified = verify_controls_build(build)
    if verified["status"] != "PASS":
        raise ControlsQualificationError(
            "controls build failed deterministic verification: "
            + " | ".join(verified["errors"])
        )

    manifest = _load_json(build / "generation-manifest.json")
    spec = parse_control_system_payload(_load_json(build / "input" / "spec.json"))
    ir = build_controls_ir(spec)

    trust_store_path = evidence_root / "trust-store.json"
    if not trust_store_path.is_file():
        raise ControlsQualificationError(
            "external qualification requires evidence/trust-store.json"
        )
    trust_store = load_trusted_signer_store(trust_store_path)
    assert trust_store is not None

    release_policy_path = evidence_root / "release-policy.json"
    release_policy = release_policy_path if release_policy_path.is_file() else None

    studio_results: list[dict[str, Any]] = []
    runtime_results: list[dict[str, Any]] = []
    for controller in ir.controllers:
        studio_results.append(
            _studio5000_qualification(
                build=build,
                evidence_root=evidence_root,
                controller=controller,
                ir=ir,
                manifest=manifest,
                trust_store=trust_store,
            )
        )

        symbol = controller_symbol(controller.id)
        controller_evidence = evidence_root / symbol
        registry = controller_evidence / "backend-registry.json"
        execution = controller_evidence / "execution-results.json"
        approval = controller_evidence / "approval.json"
        missing = [
            path.name for path in (registry, execution)
            if not path.is_file()
        ]
        if missing:
            raise ControlsQualificationError(
                f"{controller.id} runtime qualification missing: {', '.join(missing)}"
            )

        project_path = build / "rockwell" / f"{symbol}.L5X"
        requirements_path = (
            build / "requirements" / "by-controller" / f"{symbol}.json"
        )
        result = run_production_verification_v5(
            project_path,
            requirement_paths=[requirements_path],
            execution_results_path=execution,
            execution_backend_registry_path=registry,
            approval_path=approval if approval.is_file() else None,
            release_policy_path=release_policy,
            trust_store_path=trust_store_path,
        )
        readiness = result.readiness
        assert readiness is not None
        runtime_results.append(
            {
                "controller_id": controller.id,
                "readiness": readiness.status.value,
                "score": readiness.score,
                "blockers": list(readiness.blockers),
                "conditions": list(readiness.conditions),
                "backend_id": result.execution_backend_id,
                "backend_kind": result.execution_backend_kind,
                "execution_results_sha256": result.execution_results_sha256,
                "verification_context_sha256": result.verification_context_sha256,
                "verified_signatures": list(result.verified_signatures),
                "tests_total": len(result.engineering.fat_tests),
                "tests_passed": sum(
                    item.status.value == "PASS" for item in result.executions
                ),
                "human_approval": readiness.human_approval,
            }
        )

    ignition_result = _ignition_gateway_qualification(
        build=build,
        evidence_root=evidence_root,
        ir=ir,
        manifest=manifest,
        trust_store=trust_store,
    )

    runtime_ready = all(
        item["readiness"] in {
            "READY_FOR_ENGINEERING_APPROVAL",
            "APPROVED_FOR_RELEASE",
        }
        for item in runtime_results
    )
    approved = runtime_ready and all(
        item["readiness"] == "APPROVED_FOR_RELEASE"
        for item in runtime_results
    )
    status = (
        "APPROVED_FOR_RELEASE_HANDOFF"
        if approved
        else "READY_FOR_ENGINEERING_APPROVAL"
        if runtime_ready
        else "BLOCKED"
    )
    return {
        "schema": "devagent-controls-external-qualification-v1",
        "status": status,
        "project_id": spec.project_id,
        "spec_sha256": manifest["spec_sha256"],
        "controls_ir_sha256": manifest["controls_ir_sha256"],
        "build_manifest_sha256": _sha256(build / "generation-manifest.json"),
        "trust_store_sha256": trust_store.source_sha256,
        "studio5000": studio_results,
        "ignition_gateway": ignition_result,
        "runtime": runtime_results,
        "human_engineering_approval_required": True,
        "production_release_ready": approved,
        "production_deployment_performed": False,
        "deployment_authority_present": False,
    }


def write_controls_qualification(
    build_dir: Path,
    evidence_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    result = qualify_controls_build(build_dir, evidence_dir)
    target = output_path.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
