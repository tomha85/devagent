from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from devagent.controls.fat import fat_payload, generate_controls_fat, run_model_simulation
from devagent.controls.ignition import generate_ignition_payloads, ignition_binding_check
from devagent.controls.ir import ControlsIR, build_controls_ir, controls_ir_payload, controls_ir_sha256
from devagent.controls.models import ControlSystemSpec
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.parser import load_control_system_spec
from devagent.controls.rockwell import (
    ROCKWELL_GENERATOR_VERSION,
    render_rockwell_project,
)
from devagent.controls.rules import evaluate_controls_rules, rules_pass
from devagent.controls.schema import parse_control_system_payload
from devagent.controls.verification import verify_rockwell_roundtrip

BUILD_MANIFEST_SCHEMA = "devagent-controls-generation-manifest-v1"


class ControlsBuildError(ValueError):
    pass


@dataclass(frozen=True)
class ControlsBuildResult:
    output_dir: Path
    status: str
    manifest: dict[str, Any]
    readiness: dict[str, Any]


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "generation-manifest.json"
    )


def _controller_map(ir: ControlsIR) -> dict[str, Any]:
    return {item.id: item for item in ir.controllers}


def _ensure_generation_scope(ir: ControlsIR) -> None:
    unsupported = [
        f"{item.id}:{item.vendor}"
        for item in ir.controllers
        if item.vendor != "ROCKWELL"
    ]
    if unsupported:
        raise ControlsBuildError(
            "Controls Platform V1 generation is qualified for Rockwell controllers only; "
            "unsupported controller(s): " + ", ".join(unsupported)
        )


def _build_into(spec: ControlSystemSpec, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ir = build_controls_ir(spec)
    _ensure_generation_scope(ir)

    rules = evaluate_controls_rules(spec)
    if not rules_pass(rules):
        failures = [
            f"{item.id}:{item.subject}:{item.summary}"
            for item in rules
            if item.status == "FAIL"
        ]
        raise ControlsBuildError(
            "company controls standards failed: " + " | ".join(failures)
        )

    _write_json(root / "input" / "spec.json", normalized_spec_payload(spec))
    _write_json(root / "controls-ir.json", controls_ir_payload(ir))
    _write_json(
        root / "company-standards.json",
        {
            "schema": "devagent-controls-rules-v1",
            "status": "PASS",
            "results": [asdict(item) for item in rules],
        },
    )

    rockwell_manifest: list[dict[str, Any]] = []
    symbol_maps: dict[str, Any] = {}
    roundtrips: list[dict[str, Any]] = []
    controllers = _controller_map(ir)
    for controller_id in sorted(controllers):
        controller = controllers[controller_id]
        artifact = render_rockwell_project(ir, controller)
        target = root / "rockwell" / f"{controller_id}.L5X"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.content)
        symbol_maps[controller_id] = {
            equipment_id: mapping
            for equipment_id, mapping in artifact.symbol_map
        }
        rockwell_manifest.append(
            {
                "controller_id": controller_id,
                "generator_version": ROCKWELL_GENERATOR_VERSION,
                "path": target.relative_to(root).as_posix(),
                "sha256": artifact.sha256,
            }
        )
        roundtrips.append(verify_rockwell_roundtrip(ir, controller, target))

    _write_json(
        root / "rockwell" / "symbol-map.json",
        {
            "schema": "devagent-controls-rockwell-symbol-map-v1",
            "controllers": symbol_maps,
        },
    )
    _write_json(
        root / "verification" / "rockwell-roundtrip.json",
        {
            "schema": "devagent-controls-rockwell-roundtrip-suite-v1",
            "status": "PASS" if all(item["status"] == "PASS" for item in roundtrips) else "FAIL",
            "controllers": roundtrips,
        },
    )

    ignition = generate_ignition_payloads(ir)
    for name, payload in ignition.items():
        _write_json(root / "ignition" / name, payload)
    ignition_check = ignition_binding_check(ir, ignition)
    _write_json(root / "verification" / "ignition-binding.json", ignition_check)

    fat_cases = generate_controls_fat(ir)
    if not fat_cases:
        raise ControlsBuildError("no deterministic FAT cases were generated")
    _write_json(root / "tests" / "fat-plan.json", fat_payload(fat_cases))
    model_simulation = run_model_simulation(ir, fat_cases)
    _write_json(root / "tests" / "model-simulation.json", model_simulation)

    roundtrip_ok = all(item["status"] == "PASS" for item in roundtrips)
    ignition_ok = ignition_check["status"] == "PASS"
    model_ok = model_simulation["status"] == "PASS"
    static_ok = rules_pass(rules)
    authoring_ok = roundtrip_ok and ignition_ok and model_ok and static_ok

    readiness = {
        "schema": "devagent-controls-release-readiness-v1",
        "status": (
            "READY_FOR_ENGINEERING_REVIEW"
            if authoring_ok
            else "BLOCKED"
        ),
        "spec_validation": "PASS",
        "controls_ir": "PASS",
        "company_standards": "PASS" if static_ok else "FAIL",
        "deterministic_generation": "PASS",
        "rockwell_reimport": "PASS" if roundtrip_ok else "FAIL",
        "controls_ir_roundtrip": "PASS" if roundtrip_ok else "FAIL",
        "ignition_binding_coherence": "PASS" if ignition_ok else "FAIL",
        "fat_generation": "PASS",
        "deterministic_model_simulation": "PASS" if model_ok else "FAIL",
        "studio5000_import_validation": "NOT_RUN",
        "ignition_gateway_import_validation": "NOT_RUN",
        "vendor_runtime_execution": "NOT_RUN",
        "fat_execution": "NOT_RUN",
        "human_engineering_approval_required": True,
        "production_release_ready": False,
        "production_deployment_performed": False,
        "note": (
            "Authoring artifacts are ready for engineering review only. "
            "Qualified vendor/runtime execution and human approval remain required "
            "before any production release."
        ),
    }
    _write_json(root / "release-readiness.json", readiness)

    if not authoring_ok:
        raise ControlsBuildError(
            "generated controls build failed verification before manifest finalization"
        )

    artifacts = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in _relative_files(root)
    }
    manifest = {
        "schema": BUILD_MANIFEST_SCHEMA,
        "project_id": spec.project_id,
        "spec_sha256": ir.spec_sha256,
        "controls_ir_sha256": controls_ir_sha256(ir),
        "generator": {
            "rockwell": ROCKWELL_GENERATOR_VERSION,
            "ignition": "1.0.0",
            "fat": "1.0.0",
        },
        "catalog": sorted({item.standard for item in spec.equipment}),
        "artifact_class": "STAGING_ENGINEERING_BUILD",
        "external_qualification": {
            "studio5000_import_validation": "NOT_RUN",
            "ignition_gateway_import_validation": "NOT_RUN",
            "vendor_runtime_execution": "NOT_RUN",
            "fat_execution": "NOT_RUN",
        },
        "rockwell_outputs": rockwell_manifest,
        "artifact_sha256": artifacts,
        "authority": {
            "plc_write": False,
            "plc_force": False,
            "plc_download": False,
            "plc_mode_change": False,
            "live_control": False,
            "ignition_gateway_deploy": False,
        },
        "readiness": readiness["status"],
    }
    _write_json(root / "generation-manifest.json", manifest)
    return manifest, readiness


def build_controls_spec(spec: ControlSystemSpec, output_dir: Path) -> ControlsBuildResult:
    target = output_dir.expanduser().resolve(strict=False)
    if target.exists():
        raise ControlsBuildError(f"output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=str(target.parent))
    )
    try:
        manifest, readiness = _build_into(spec, temporary)
        verify_result = verify_controls_build(temporary)
        if verify_result["status"] != "PASS":
            raise ControlsBuildError(
                "post-build self-verification failed: "
                + " | ".join(verify_result["errors"])
            )
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return ControlsBuildResult(
        output_dir=target,
        status="PASS",
        manifest=manifest,
        readiness=readiness,
    )


def build_controls_project(spec_path: Path, output_dir: Path) -> ControlsBuildResult:
    return build_controls_spec(load_control_system_spec(spec_path), output_dir)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlsBuildError(f"invalid build JSON artifact {path}: {exc}") from exc


def verify_controls_build(build_dir: Path) -> dict[str, Any]:
    root = build_dir.expanduser().resolve(strict=True)
    manifest_path = root / "generation-manifest.json"
    if not manifest_path.is_file():
        return {"status": "FAIL", "errors": ["generation-manifest.json is missing"]}

    try:
        manifest = _load_json(manifest_path)
        if manifest.get("schema") != BUILD_MANIFEST_SCHEMA:
            raise ControlsBuildError("unsupported generation manifest schema")

        expected_artifacts = dict(manifest.get("artifact_sha256", {}))
        actual_paths = {
            path.relative_to(root).as_posix()
            for path in _relative_files(root)
        }
        errors: list[str] = []
        if set(expected_artifacts) != actual_paths:
            errors.append(
                "artifact set mismatch missing="
                + repr(sorted(set(expected_artifacts) - actual_paths))
                + " extra="
                + repr(sorted(actual_paths - set(expected_artifacts)))
            )
        for relative, expected_sha in sorted(expected_artifacts.items()):
            path = root / relative
            if path.is_file():
                actual_sha = _sha256(path)
                if actual_sha != expected_sha:
                    errors.append(
                        f"artifact hash mismatch {relative}: {actual_sha} != {expected_sha}"
                    )

        spec_payload = _load_json(root / "input" / "spec.json")
        spec = parse_control_system_payload(spec_payload)
        ir = build_controls_ir(spec)
        if manifest.get("spec_sha256") != ir.spec_sha256:
            errors.append("spec_sha256 does not match canonical input")
        if manifest.get("controls_ir_sha256") != controls_ir_sha256(ir):
            errors.append("controls_ir_sha256 does not match canonical input")

        rules = evaluate_controls_rules(spec)
        if not rules_pass(rules):
            errors.append("company controls standards no longer pass")

        _ensure_generation_scope(ir)
        for controller in ir.controllers:
            target = root / "rockwell" / f"{controller.id}.L5X"
            expected = render_rockwell_project(ir, controller)
            if not target.is_file():
                errors.append(f"missing Rockwell artifact for {controller.id}")
                continue
            if target.read_bytes() != expected.content:
                errors.append(
                    f"Rockwell artifact for {controller.id} is not byte-deterministic"
                )
            roundtrip = verify_rockwell_roundtrip(ir, controller, target)
            if roundtrip["status"] != "PASS":
                errors.extend(
                    f"{controller.id}: {message}"
                    for message in roundtrip["mismatches"]
                )

        ignition = generate_ignition_payloads(ir)
        for name, expected in ignition.items():
            path = root / "ignition" / name
            if not path.is_file() or _load_json(path) != expected:
                errors.append(f"Ignition staging artifact mismatch: {name}")
        ignition_check = ignition_binding_check(ir, ignition)
        if ignition_check["status"] != "PASS":
            errors.append("Ignition binding coherence failed")

        cases = generate_controls_fat(ir)
        if _load_json(root / "tests" / "fat-plan.json") != fat_payload(cases):
            errors.append("FAT plan does not match deterministic controls intent")
        simulation = run_model_simulation(ir, cases)
        if simulation["status"] != "PASS":
            errors.append("deterministic model simulation failed")
        if _load_json(root / "tests" / "model-simulation.json") != simulation:
            errors.append("model simulation artifact mismatch")

        readiness = _load_json(root / "release-readiness.json")
        if readiness.get("production_release_ready") is not False:
            errors.append("authoring build must never claim production release readiness")
        if readiness.get("fat_execution") != "NOT_RUN":
            errors.append("authoring build must not claim external FAT execution")
        if readiness.get("human_engineering_approval_required") is not True:
            errors.append("human engineering approval must remain required")

        return {
            "status": "PASS" if not errors else "FAIL",
            "errors": errors,
            "project_id": spec.project_id,
            "spec_sha256": ir.spec_sha256,
            "controls_ir_sha256": controls_ir_sha256(ir),
        }
    except (ControlsBuildError, OSError, ValueError) as exc:
        return {"status": "FAIL", "errors": [str(exc)]}
