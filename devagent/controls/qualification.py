from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from devagent.controls.build import ControlsBuildError, verify_controls_build
from devagent.controls.ignition import (
    ignition_semantic_projection,
    normalize_ignition_projection,
)
from devagent.controls.ir import build_controls_ir, controls_ir_sha256
from devagent.controls.schema import parse_control_system_payload
from devagent.controls.rules import release_io_mapping_check
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


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
        "import_evidence_sha256": _sha256(metadata_path),
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
    export_evidence_path = evidence_root / "ignition-gateway-export.json"
    if not evidence_path.is_file() or not export_evidence_path.is_file():
        raise ControlsQualificationError(
            "Ignition qualification requires signed ignition-gateway-import.json "
            "and ignition-gateway-export.json"
        )

    metadata, import_signature = _signed_snapshot(
        evidence_path,
        purpose="CONTROLS_IGNITION_GATEWAY_IMPORT",
        trust_store=trust_store,
    )
    if metadata.get("schema") != "devagent-controls-ignition-gateway-import-evidence-v1":
        raise ControlsQualificationError("Ignition Gateway import evidence has unsupported schema")
    if metadata.get("status") != "PASS":
        raise ControlsQualificationError("Ignition Gateway import evidence status must be PASS")
    if metadata.get("controls_ir_sha256") != controls_ir_sha256(ir):
        raise ControlsQualificationError(
            "Ignition Gateway import evidence Controls IR hash mismatch"
        )

    expected_hashes = {
        relative: digest
        for relative, digest in manifest["artifact_sha256"].items()
        if relative.startswith("ignition/")
    }
    supplied = metadata.get("artifact_sha256")
    if supplied != expected_hashes:
        raise ControlsQualificationError(
            "Ignition Gateway import evidence artifact_sha256 does not exactly match "
            "the verified staging artifacts"
        )
    gateway_version = str(metadata.get("gateway_version") or "").strip()
    gateway_id = str(metadata.get("gateway_id") or "").strip()
    if not gateway_version or not gateway_id:
        raise ControlsQualificationError(
            "Ignition Gateway import evidence requires gateway_id and gateway_version"
        )
    imported_at = _timestamp(
        metadata.get("imported_at"),
        field="Ignition Gateway imported_at",
    )

    staging_payloads = {
        name: _load_json(build / "ignition" / name)
        for name in (
            "udts.json",
            "equipment.json",
            "alarms.json",
            "history.json",
            "views.json",
            "navigation.json",
        )
    }
    expected_projection = ignition_semantic_projection(staging_payloads)
    expected_projection_sha256 = _json_sha256(expected_projection)

    export_metadata, export_signature = _signed_snapshot(
        export_evidence_path,
        purpose="CONTROLS_IGNITION_GATEWAY_EXPORT",
        trust_store=trust_store,
    )
    if (
        export_metadata.get("schema")
        != "devagent-controls-ignition-gateway-export-evidence-v1"
    ):
        raise ControlsQualificationError("Ignition Gateway export evidence has unsupported schema")
    if export_metadata.get("status") != "PASS":
        raise ControlsQualificationError("Ignition Gateway export evidence status must be PASS")
    if export_metadata.get("controls_ir_sha256") != controls_ir_sha256(ir):
        raise ControlsQualificationError(
            "Ignition Gateway export evidence Controls IR hash mismatch"
        )
    if str(export_metadata.get("gateway_version") or "").strip() != gateway_version:
        raise ControlsQualificationError(
            "Ignition Gateway import/export evidence gateway_version mismatch"
        )
    if str(export_metadata.get("gateway_id") or "").strip() != gateway_id:
        raise ControlsQualificationError(
            "Ignition Gateway import/export evidence gateway_id mismatch"
        )
    adapter = str(export_metadata.get("adapter") or "").strip()
    adapter_version = str(export_metadata.get("adapter_version") or "").strip()
    if not adapter or not adapter_version:
        raise ControlsQualificationError(
            "Ignition Gateway export evidence requires adapter and adapter_version"
        )
    exported_at = _timestamp(
        export_metadata.get("exported_at"),
        field="Ignition Gateway exported_at",
    )
    raw_projection = export_metadata.get("projection")
    if not isinstance(raw_projection, dict):
        raise ControlsQualificationError(
            "Ignition Gateway export evidence projection must be a JSON object"
        )
    try:
        actual_projection = normalize_ignition_projection(raw_projection)
    except ValueError as exc:
        raise ControlsQualificationError(
            f"invalid Ignition Gateway semantic projection: {exc}"
        ) from exc
    actual_projection_sha256 = _json_sha256(actual_projection)
    if export_metadata.get("projection_sha256") != actual_projection_sha256:
        raise ControlsQualificationError(
            "Ignition Gateway export evidence projection_sha256 mismatch"
        )
    if actual_projection != expected_projection:
        raise ControlsQualificationError(
            "Ignition Gateway exported semantic projection does not match "
            "the deterministic staging intent"
        )

    return {
        "status": "PASS",
        "gateway_id": gateway_id,
        "gateway_version": gateway_version,
        "imported_at": imported_at,
        "exported_at": exported_at,
        "artifact_sha256": expected_hashes,
        "evidence_mode": "SIGNED_IMPORT_AND_SEMANTIC_EXPORT_EVIDENCE",
        "semantic_gateway_export_reimport": "PASS",
        "expected_projection_sha256": expected_projection_sha256,
        "actual_projection_sha256": actual_projection_sha256,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "import_evidence_sha256": _sha256(evidence_path),
        "export_evidence_sha256": _sha256(export_evidence_path),
        "import_signature": import_signature,
        "export_signature": export_signature,
    }


def _controls_fat_qualification(
    *,
    build: Path,
    evidence_root: Path,
    ir,
    manifest: dict[str, Any],
    trust_store,
    runtime_bindings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence_path = evidence_root / "controls-fat-results.json"
    if not evidence_path.is_file():
        raise ControlsQualificationError(
            "integrated Controls FAT requires signed controls-fat-results.json"
        )
    metadata, signature = _signed_snapshot(
        evidence_path,
        purpose="CONTROLS_FAT_RESULTS",
        trust_store=trust_store,
    )
    if metadata.get("schema") != "devagent-controls-fat-results-v1":
        raise ControlsQualificationError("Controls FAT evidence has unsupported schema")
    if metadata.get("status") != "PASS":
        raise ControlsQualificationError("Controls FAT evidence status must be PASS")
    if metadata.get("controls_ir_sha256") != controls_ir_sha256(ir):
        raise ControlsQualificationError("Controls FAT evidence Controls IR hash mismatch")

    manifest_sha256 = _sha256(build / "generation-manifest.json")
    if metadata.get("generation_manifest_sha256") != manifest_sha256:
        raise ControlsQualificationError(
            "Controls FAT evidence generation_manifest_sha256 mismatch"
        )
    fat_plan_path = build / "tests" / "fat-plan.json"
    fat_plan_sha256 = _sha256(fat_plan_path)
    if metadata.get("fat_plan_sha256") != fat_plan_sha256:
        raise ControlsQualificationError(
            "Controls FAT evidence fat_plan_sha256 mismatch"
        )

    plan = _load_json(fat_plan_path)
    planned_cases = plan.get("cases", [])
    if not isinstance(planned_cases, list) or not all(
        isinstance(item, dict) for item in planned_cases
    ):
        raise ControlsQualificationError("Controls FAT plan cases must be objects")
    expected_ids = [str(item["id"]) for item in planned_cases]
    if not expected_ids:
        raise ControlsQualificationError("Controls FAT plan has no cases")
    if len(expected_ids) != len(set(expected_ids)):
        raise ControlsQualificationError("Controls FAT plan contains duplicate test ids")
    planned_by_id = {str(item["id"]): item for item in planned_cases}

    results = metadata.get("results")
    if not isinstance(results, list):
        raise ControlsQualificationError("Controls FAT evidence results must be a list")
    result_ids = [str(item.get("test_id") or "") for item in results if isinstance(item, dict)]
    if len(result_ids) != len(results) or any(not item for item in result_ids):
        raise ControlsQualificationError(
            "Controls FAT evidence contains an invalid test_id"
        )
    if len(result_ids) != len(set(result_ids)):
        raise ControlsQualificationError(
            "Controls FAT evidence contains duplicate test ids"
        )
    if set(result_ids) != set(expected_ids):
        raise ControlsQualificationError(
            "Controls FAT evidence test set does not exactly match generated FAT plan"
        )

    failures: list[str] = []
    for item in results:
        test_id = str(item["test_id"])
        planned = planned_by_id[test_id]
        if item.get("status") != "PASS":
            failures.append(f"{test_id}:{item.get('status')}")
        _timestamp(
            item.get("timestamp"),
            field=f"Controls FAT {test_id} timestamp",
        )
        observed = str(item.get("observed") or "").strip()
        evidence = item.get("evidence")
        if not observed:
            raise ControlsQualificationError(
                f"Controls FAT {test_id} observed result is required"
            )
        observed_value = item.get("observed_value")
        if not isinstance(observed_value, bool):
            raise ControlsQualificationError(
                f"Controls FAT {test_id} observed_value must be Boolean"
            )
        expected_value = planned.get("expected_value")
        if not isinstance(expected_value, bool):
            raise ControlsQualificationError(
                f"Controls FAT plan {test_id} expected_value must be Boolean"
            )
        if observed_value != expected_value:
            raise ControlsQualificationError(
                f"Controls FAT {test_id} observed_value {observed_value} "
                f"does not match expected_value {expected_value}"
            )
        observed_after_ms = item.get("observed_after_ms")
        if isinstance(observed_after_ms, bool) or not isinstance(observed_after_ms, int):
            raise ControlsQualificationError(
                f"Controls FAT {test_id} observed_after_ms must be an integer"
            )
        if observed_after_ms < 0:
            raise ControlsQualificationError(
                f"Controls FAT {test_id} observed_after_ms must be non-negative"
            )
        evaluation_delay_ms = planned.get("evaluation_delay_ms", 0)
        if (
            isinstance(evaluation_delay_ms, bool)
            or not isinstance(evaluation_delay_ms, int)
            or evaluation_delay_ms < 0
        ):
            raise ControlsQualificationError(
                f"Controls FAT plan {test_id} has invalid evaluation_delay_ms"
            )
        if observed_after_ms < evaluation_delay_ms:
            raise ControlsQualificationError(
                f"Controls FAT {test_id} was observed at {observed_after_ms} ms "
                f"before required evaluation delay {evaluation_delay_ms} ms"
            )
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(value, str) and value.strip() for value in evidence
        ):
            raise ControlsQualificationError(
                f"Controls FAT {test_id} requires non-empty evidence references"
            )
    if failures:
        raise ControlsQualificationError(
            "Controls FAT contains failing test(s): " + ", ".join(sorted(failures))
        )

    supplied_bindings = metadata.get("runtime_bindings")
    if supplied_bindings != runtime_bindings:
        raise ControlsQualificationError(
            "Controls FAT runtime_bindings do not exactly match the qualified "
            "controller execution contexts"
        )
    expected_backend_ids = sorted(
        {
            str(value["backend_id"])
            for value in runtime_bindings.values()
            if value.get("backend_id")
        }
    )
    run_id = str(metadata.get("run_id") or "").strip()
    if not run_id:
        raise ControlsQualificationError("Controls FAT evidence run_id is required")
    executed_by = str(metadata.get("executed_by") or "").strip()
    environment_id = str(metadata.get("environment_id") or "").strip()
    if not executed_by or not environment_id:
        raise ControlsQualificationError(
            "Controls FAT evidence requires executed_by and environment_id"
        )
    executed_at = _timestamp(
        metadata.get("executed_at"),
        field="Controls FAT executed_at",
    )
    return {
        "status": "PASS",
        "run_id": run_id,
        "executed_at": executed_at,
        "executed_by": executed_by,
        "environment_id": environment_id,
        "fat_plan_sha256": fat_plan_sha256,
        "generation_manifest_sha256": manifest_sha256,
        "runtime_backend_ids": expected_backend_ids,
        "runtime_bindings": runtime_bindings,
        "tests_total": len(expected_ids),
        "tests_passed": len(expected_ids),
        "evidence_sha256": _sha256(evidence_path),
        "signature": signature,
    }


def _controls_approval_context(
    *,
    manifest: dict[str, Any],
    build: Path,
    trust_store_sha256: str,
    studio_results: list[dict[str, Any]],
    ignition_result: dict[str, Any],
    integrated_fat_result: dict[str, Any],
    runtime_results: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    context = {
        "schema": "devagent-controls-engineering-approval-context-v1",
        "project_id": manifest["project_id"],
        "spec_sha256": manifest["spec_sha256"],
        "controls_ir_sha256": manifest["controls_ir_sha256"],
        "generation_manifest_sha256": _sha256(build / "generation-manifest.json"),
        "trust_store_sha256": trust_store_sha256,
        "studio5000": {
            str(item["controller_id"]): {
                "generated_project_sha256": item["generated_project_sha256"],
                "exported_project_sha256": item["exported_project_sha256"],
                "expected_semantic_sha256": item["expected_semantic_sha256"],
                "actual_semantic_sha256": item["actual_semantic_sha256"],
                "import_evidence_sha256": item["import_evidence_sha256"],
            }
            for item in studio_results
        },
        "ignition_gateway": {
            "expected_projection_sha256": ignition_result["expected_projection_sha256"],
            "actual_projection_sha256": ignition_result["actual_projection_sha256"],
            "import_evidence_sha256": ignition_result["import_evidence_sha256"],
            "export_evidence_sha256": ignition_result["export_evidence_sha256"],
        },
        "integrated_fat": {
            "run_id": integrated_fat_result["run_id"],
            "fat_plan_sha256": integrated_fat_result["fat_plan_sha256"],
            "evidence_sha256": integrated_fat_result["evidence_sha256"],
            "runtime_bindings": integrated_fat_result["runtime_bindings"],
        },
        "runtime": {
            str(item["controller_id"]): {
                "readiness": item["readiness"],
                "backend_id": item["backend_id"],
                "execution_results_sha256": item["execution_results_sha256"],
                "verification_context_sha256": item["verification_context_sha256"],
            }
            for item in runtime_results
        },
    }
    return context, _json_sha256(context)


def _controls_engineering_approval(
    *,
    evidence_root: Path,
    trust_store,
    manifest: dict[str, Any],
    approval_context_sha256: str,
) -> dict[str, Any]:
    approval_path = evidence_root / "controls-engineering-approval.json"
    if not approval_path.is_file():
        return {
            "status": "REQUIRED",
            "approval_context_sha256": approval_context_sha256,
            "decision": None,
            "approved_by": None,
            "approved_at": None,
            "signature": None,
        }

    payload, signature = _signed_snapshot(
        approval_path,
        purpose="CONTROLS_ENGINEERING_APPROVAL",
        trust_store=trust_store,
    )
    if payload.get("schema") != "devagent-controls-engineering-approval-v1":
        raise ControlsQualificationError(
            "Controls engineering approval has unsupported schema"
        )
    if payload.get("project_id") != manifest["project_id"]:
        raise ControlsQualificationError(
            "Controls engineering approval project_id mismatch"
        )
    if payload.get("spec_sha256") != manifest["spec_sha256"]:
        raise ControlsQualificationError(
            "Controls engineering approval spec_sha256 mismatch"
        )
    if payload.get("controls_ir_sha256") != manifest["controls_ir_sha256"]:
        raise ControlsQualificationError(
            "Controls engineering approval Controls IR hash mismatch"
        )
    if payload.get("approval_context_sha256") != approval_context_sha256:
        raise ControlsQualificationError(
            "Controls engineering approval context hash mismatch"
        )
    if payload.get("decision") != "APPROVE":
        raise ControlsQualificationError(
            "Controls engineering approval decision must be APPROVE"
        )
    approved_by = str(payload.get("approved_by") or "").strip()
    if not approved_by:
        raise ControlsQualificationError(
            "Controls engineering approval approved_by is required"
        )
    approved_at = _timestamp(
        payload.get("approved_at"),
        field="Controls engineering approval approved_at",
    )
    return {
        "status": "APPROVED",
        "approval_context_sha256": approval_context_sha256,
        "decision": "APPROVE",
        "approved_by": approved_by,
        "approved_at": approved_at,
        "evidence_sha256": _sha256(approval_path),
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

    release_io = release_io_mapping_check(spec)
    if release_io["status"] != "PASS":
        details = "; ".join(
            f"{equipment_id}: {', '.join(members)}"
            for equipment_id, members in sorted(release_io["missing"].items())
        )
        raise ControlsQualificationError(
            "external qualification requires complete release I/O mapping for "
            "all required feedback signals and generated outputs: " + details
        )

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
    integrated_fat_result = _controls_fat_qualification(
        build=build,
        evidence_root=evidence_root,
        ir=ir,
        manifest=manifest,
        trust_store=trust_store,
        runtime_bindings={
            str(item["controller_id"]): {
                "backend_id": item["backend_id"],
                "execution_results_sha256": item["execution_results_sha256"],
                "verification_context_sha256": item["verification_context_sha256"],
            }
            for item in runtime_results
        },
    )

    external_vendor_ready = (
        all(item["status"] == "PASS" for item in studio_results)
        and ignition_result["status"] == "PASS"
        and integrated_fat_result["status"] == "PASS"
    )
    runtime_ready = external_vendor_ready and all(
        item["readiness"] in {
            "READY_FOR_ENGINEERING_APPROVAL",
            "APPROVED_FOR_RELEASE",
        }
        for item in runtime_results
    )
    controller_approvals_complete = all(
        item["readiness"] == "APPROVED_FOR_RELEASE"
        for item in runtime_results
    )

    approval_context, approval_context_sha256 = _controls_approval_context(
        manifest=manifest,
        build=build,
        trust_store_sha256=trust_store.source_sha256,
        studio_results=studio_results,
        ignition_result=ignition_result,
        integrated_fat_result=integrated_fat_result,
        runtime_results=runtime_results,
    )
    engineering_approval = _controls_engineering_approval(
        evidence_root=evidence_root,
        trust_store=trust_store,
        manifest=manifest,
        approval_context_sha256=approval_context_sha256,
    )
    approved = (
        runtime_ready
        and controller_approvals_complete
        and engineering_approval["status"] == "APPROVED"
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
        "integrated_fat": integrated_fat_result,
        "release_io_mapping": release_io,
        "runtime": runtime_results,
        "approval_context": approval_context,
        "approval_context_sha256": approval_context_sha256,
        "engineering_approval": engineering_approval,
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
    build = build_dir.expanduser().resolve(strict=True)
    result = qualify_controls_build(build, evidence_dir)
    target = output_path.expanduser().resolve(strict=False)
    if target == build or build in target.parents:
        raise ControlsQualificationError(
            "qualification output must be outside the immutable verified build directory"
        )
    if target.exists():
        raise ControlsQualificationError(
            f"qualification output already exists: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
