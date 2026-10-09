from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from devagent.controls.fat import (
    FAT_GENERATOR_VERSION,
    fat_payload,
    generate_controls_fat,
    run_model_simulation,
)
from devagent.controls.ignition import (
    IGNITION_GENERATOR_VERSION,
    generate_ignition_payloads,
    ignition_binding_check,
)
from devagent.controls.ir import ControlsIR, build_controls_ir, controls_ir_payload, controls_ir_sha256
from devagent.controls.models import ControlSystemSpec
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.parser import load_control_system_spec
from devagent.controls.requirements import hmi_requirement_rows, requirements_payload
from devagent.controls.rockwell import (
    ROCKWELL_GENERATOR_VERSION,
    render_rockwell_project,
    rockwell_reference_provenance,
)
from devagent.controls.rules import (
    evaluate_controls_rules,
    release_io_mapping_check,
    rules_pass,
)
from devagent.controls.schema import parse_control_system_payload
from devagent.controls.symbols import controller_symbol
from devagent.controls.verification import verify_rockwell_roundtrip

BUILD_MANIFEST_SCHEMA = "devagent-controls-generation-manifest-v2"


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
    generated_controller_names = [controller_symbol(item.id) for item in ir.controllers]
    if len(generated_controller_names) != len(set(generated_controller_names)):
        raise ControlsBuildError(
            "controller identities collide after Rockwell normalization"
        )

    unsupported = [
        f"{item.id}:{item.vendor}"
        for item in ir.controllers
        if item.vendor != "ROCKWELL"
    ]
    if unsupported:
        raise ControlsBuildError(
            "Controls Platform generation is qualified for Rockwell controllers only; "
            "unsupported controller(s): " + ", ".join(unsupported)
        )

    unqualified_platforms = [
        f"{item.id}:{item.platform}"
        for item in ir.controllers
        if item.platform.strip().upper().replace(" ", "") not in {"CONTROLLOGIX", "1756"}
    ]
    if unqualified_platforms:
        raise ControlsBuildError(
            "Controls Platform generation requires a pinned Studio-5000-exported "
            "golden template; V2 currently qualifies CONTROLLOGIX only. "
            "Unqualified controller platform(s): " + ", ".join(unqualified_platforms)
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
            "schema": "devagent-controls-rules-v2",
            "status": "PASS",
            "results": [asdict(item) for item in rules],
        },
    )
    _write_json(
        root / "io-map.json",
        {
            "schema": "devagent-controls-io-map-v1",
            "project_id": spec.project_id,
            "mappings": [
                {
                    "equipment_id": item.id,
                    "controller_id": item.controller,
                    "area": item.area,
                    "safety_zone": item.safety_zone,
                    "member": mapping.member,
                    "direction": mapping.direction,
                    "address": mapping.address,
                }
                for item in ir.equipment
                for mapping in item.io
            ],
            "direct_physical_output_generation": False,
        },
    )
    io_coverage = release_io_mapping_check(spec)
    _write_json(root / "verification" / "io-coverage.json", io_coverage)

    rockwell_manifest: list[dict[str, Any]] = []
    symbol_maps: dict[str, Any] = {}
    roundtrips: list[dict[str, Any]] = []
    controllers = _controller_map(ir)
    for controller_id in sorted(controllers):
        controller = controllers[controller_id]
        artifact = render_rockwell_project(ir, controller)
        target = root / "rockwell" / f"{controller_symbol(controller_id)}.L5X"
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

    requirement_payload = requirements_payload(ir)
    _write_json(
        root / "requirements" / "controls-requirements.json",
        requirement_payload,
    )
    for controller in ir.controllers:
        _write_json(
            root / "requirements" / "by-controller"
            / f"{controller_symbol(controller.id)}.json",
            requirements_payload(ir, controller_id=controller.id),
        )
    _write_json(
        root / "requirements" / "hmi-requirements.json",
        {
            "schema": "devagent-controls-hmi-requirements-v1",
            "requirements": hmi_requirement_rows(ir),
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

    structured_requirements = {
        (item.id, requirement.id)
        for item in ir.equipment
        for requirement in item.requirements
        if requirement.assertion is not None
    }
    requirement_counts: dict[tuple[str, str], int] = {}
    for case in fat_cases:
        for requirement_id in case.requirement_ids:
            key = (case.equipment_id, requirement_id)
            requirement_counts[key] = requirement_counts.get(key, 0) + 1
    bad_requirement_coverage = {
        f"{equipment_id}:{requirement_id}": requirement_counts.get(
            (equipment_id, requirement_id), 0
        )
        for equipment_id, requirement_id in sorted(structured_requirements)
        if requirement_counts.get((equipment_id, requirement_id), 0) != 1
    }
    if bad_requirement_coverage:
        raise ControlsBuildError(
            "structured requirement FAT coverage must equal one case per assertion: "
            + ", ".join(
                f"{key}={count}"
                for key, count in sorted(bad_requirement_coverage.items())
            )
        )

    expected_alarm_refs = {
        (item.id, f"ALARM.{alarm.id}")
        for item in ir.equipment
        for alarm in item.alarms
    }
    actual_alarm_refs = {
        (case.equipment_id, case.expected_output)
        for case in fat_cases
        if case.expected_output.startswith("ALARM.")
    }
    if expected_alarm_refs != actual_alarm_refs:
        raise ControlsBuildError(
            "generated FAT alarm coverage differs from declared alarm contract"
        )
    fat_coverage = {
        "schema": "devagent-controls-fat-coverage-v1",
        "status": "PASS",
        "structured_requirements": [
            {
                "equipment_id": equipment_id,
                "requirement_id": requirement_id,
                "case_count": requirement_counts[(equipment_id, requirement_id)],
            }
            for equipment_id, requirement_id in sorted(structured_requirements)
        ],
        "alarms": [
            {"equipment_id": equipment_id, "expected_ref": expected_ref}
            for equipment_id, expected_ref in sorted(expected_alarm_refs)
        ],
    }
    _write_json(root / "verification" / "fat-coverage.json", fat_coverage)

    _write_json(root / "tests" / "fat-plan.json", fat_payload(fat_cases))
    model_simulation = run_model_simulation(ir, fat_cases)
    _write_json(root / "tests" / "model-simulation.json", model_simulation)

    _write_json(
        root / "engineering-handoff.json",
        {
            "schema": "devagent-controls-engineering-handoff-v2",
            "project_id": spec.project_id,
            "controls_ir_sha256": controls_ir_sha256(ir),
            "plc_review": [
                {
                    "controller_id": item["controller_id"],
                    "project_path": item["path"],
                    "requirements_path": (
                        "requirements/by-controller/"
                        f"{controller_symbol(item['controller_id'])}.json"
                    ),
                    "next_command": (
                        f"devagent plc {item['path']} "
                        "--requirements requirements/by-controller/"
                        f"{controller_symbol(item['controller_id'])}.json "
                        "--output-dir <engineer-selected-output>"
                    ),
                }
                for item in rockwell_manifest
            ],
            "ignition_staging_path": "ignition/",
            "fat_plan_path": "tests/fat-plan.json",
            "qualified_runtime_evidence_required": True,
            "human_engineering_approval_required": True,
            "production_deployment_performed": False,
        },
    )

    roundtrip_ok = all(item["status"] == "PASS" for item in roundtrips)
    ignition_ok = ignition_check["status"] == "PASS"
    model_ok = model_simulation["status"] == "PASS"
    static_ok = rules_pass(rules)
    authoring_ok = roundtrip_ok and ignition_ok and model_ok and static_ok

    readiness = {
        "schema": "devagent-controls-release-readiness-v2",
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
        "release_io_mapping": (
            "PASS" if io_coverage["status"] == "PASS" else "REVIEW_REQUIRED"
        ),
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
            "ignition": IGNITION_GENERATOR_VERSION,
            "fat": FAT_GENERATOR_VERSION,
        },
        "catalog": sorted({item.standard for item in spec.equipment}),
        "artifact_class": "STAGING_ENGINEERING_BUILD",
        "external_qualification": {
            "studio5000_import_validation": "NOT_RUN",
            "ignition_gateway_import_validation": "NOT_RUN",
            "vendor_runtime_execution": "NOT_RUN",
            "fat_execution": "NOT_RUN",
        },
        "rockwell_reference": rockwell_reference_provenance(),
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
        if spec_payload != normalized_spec_payload(spec):
            errors.append("input/spec.json is not the canonical normalized specification")
        if _load_json(root / "controls-ir.json") != controls_ir_payload(ir):
            errors.append("controls-ir.json does not match deterministic Controls IR")
        if manifest.get("catalog") != sorted({item.standard for item in spec.equipment}):
            errors.append("generation manifest catalog does not match specification")
        if manifest.get("artifact_class") != "STAGING_ENGINEERING_BUILD":
            errors.append("generation manifest artifact_class is not staging-only")
        if manifest.get("generator") != {
            "rockwell": ROCKWELL_GENERATOR_VERSION,
            "ignition": IGNITION_GENERATOR_VERSION,
            "fat": FAT_GENERATOR_VERSION,
        }:
            errors.append("generation manifest generator versions do not match this build engine")
        if manifest.get("rockwell_reference") != rockwell_reference_provenance():
            errors.append("generation manifest Rockwell reference provenance is invalid")
        if manifest.get("external_qualification") != {
            "studio5000_import_validation": "NOT_RUN",
            "ignition_gateway_import_validation": "NOT_RUN",
            "vendor_runtime_execution": "NOT_RUN",
            "fat_execution": "NOT_RUN",
        }:
            errors.append("generation manifest external qualification boundary is invalid")
        if manifest.get("authority") != {
            "plc_write": False,
            "plc_force": False,
            "plc_download": False,
            "plc_mode_change": False,
            "live_control": False,
            "ignition_gateway_deploy": False,
        }:
            errors.append("generation manifest authority boundary is invalid")

        rules = evaluate_controls_rules(spec)
        if not rules_pass(rules):
            errors.append("company controls standards no longer pass")
        expected_rules = {
            "schema": "devagent-controls-rules-v2",
            "status": "PASS",
            "results": [asdict(item) for item in rules],
        }
        if _load_json(root / "company-standards.json") != expected_rules:
            errors.append("company-standards.json does not match deterministic rules")

        expected_requirements = requirements_payload(ir)
        if _load_json(root / "requirements" / "controls-requirements.json") != expected_requirements:
            errors.append("generated requirements handoff does not match Controls IR")
        for controller in ir.controllers:
            expected_controller_requirements = requirements_payload(
                ir, controller_id=controller.id
            )
            controller_requirements_path = (
                root / "requirements" / "by-controller"
                / f"{controller_symbol(controller.id)}.json"
            )
            if (
                not controller_requirements_path.is_file()
                or _load_json(controller_requirements_path)
                != expected_controller_requirements
            ):
                errors.append(
                    f"controller requirements handoff mismatch for {controller.id}"
                )
        expected_hmi_requirements = {
            "schema": "devagent-controls-hmi-requirements-v1",
            "requirements": hmi_requirement_rows(ir),
        }
        if (
            _load_json(root / "requirements" / "hmi-requirements.json")
            != expected_hmi_requirements
        ):
            errors.append("HMI requirements handoff does not match Controls IR")

        expected_io_map = {
            "schema": "devagent-controls-io-map-v1",
            "project_id": spec.project_id,
            "mappings": [
                {
                    "equipment_id": item.id,
                    "controller_id": item.controller,
                    "area": item.area,
                    "safety_zone": item.safety_zone,
                    "member": mapping.member,
                    "direction": mapping.direction,
                    "address": mapping.address,
                }
                for item in ir.equipment
                for mapping in item.io
            ],
            "direct_physical_output_generation": False,
        }
        if _load_json(root / "io-map.json") != expected_io_map:
            errors.append("io-map.json does not match deterministic Controls IR")
        expected_io_coverage = release_io_mapping_check(spec)
        if _load_json(root / "verification" / "io-coverage.json") != expected_io_coverage:
            errors.append("verification/io-coverage.json does not match Controls IR")

        _ensure_generation_scope(ir)
        for controller in ir.controllers:
            target = root / "rockwell" / f"{controller_symbol(controller.id)}.L5X"
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
        if readiness.get("status") != "READY_FOR_ENGINEERING_REVIEW":
            errors.append("verified authoring build must be ready for engineering review")
        if manifest.get("readiness") != readiness.get("status"):
            errors.append("manifest/readiness status mismatch")
        for field in (
            "studio5000_import_validation",
            "ignition_gateway_import_validation",
            "vendor_runtime_execution",
            "fat_execution",
        ):
            if readiness.get(field) != "NOT_RUN":
                errors.append(f"authoring build must keep {field}=NOT_RUN")
        if readiness.get("production_release_ready") is not False:
            errors.append("authoring build must never claim production release readiness")
        if readiness.get("production_deployment_performed") is not False:
            errors.append("authoring build must never claim production deployment")
        if readiness.get("human_engineering_approval_required") is not True:
            errors.append("human engineering approval must remain required")

        expected_fat_cases = generate_controls_fat(ir)
        structured_requirements = {
            (item.id, requirement.id)
            for item in ir.equipment
            for requirement in item.requirements
            if requirement.assertion is not None
        }
        requirement_counts: dict[tuple[str, str], int] = {}
        for case in expected_fat_cases:
            for requirement_id in case.requirement_ids:
                key = (case.equipment_id, requirement_id)
                requirement_counts[key] = requirement_counts.get(key, 0) + 1
        expected_alarm_refs = {
            (item.id, f"ALARM.{alarm.id}")
            for item in ir.equipment
            for alarm in item.alarms
        }
        expected_fat_coverage = {
            "schema": "devagent-controls-fat-coverage-v1",
            "status": "PASS",
            "structured_requirements": [
                {
                    "equipment_id": equipment_id,
                    "requirement_id": requirement_id,
                    "case_count": requirement_counts.get(
                        (equipment_id, requirement_id), 0
                    ),
                }
                for equipment_id, requirement_id in sorted(structured_requirements)
            ],
            "alarms": [
                {"equipment_id": equipment_id, "expected_ref": expected_ref}
                for equipment_id, expected_ref in sorted(expected_alarm_refs)
            ],
        }
        if _load_json(root / "verification" / "fat-coverage.json") != expected_fat_coverage:
            errors.append("fat-coverage.json does not match deterministic Controls intent")

        expected_handoff = {
            "schema": "devagent-controls-engineering-handoff-v2",
            "project_id": spec.project_id,
            "controls_ir_sha256": controls_ir_sha256(ir),
            "plc_review": [
                {
                    "controller_id": controller.id,
                    "project_path": (
                        f"rockwell/{controller_symbol(controller.id)}.L5X"
                    ),
                    "requirements_path": (
                        "requirements/by-controller/"
                        f"{controller_symbol(controller.id)}.json"
                    ),
                    "next_command": (
                        f"devagent plc rockwell/{controller_symbol(controller.id)}.L5X "
                        "--requirements requirements/by-controller/"
                        f"{controller_symbol(controller.id)}.json "
                        "--output-dir <engineer-selected-output>"
                    ),
                }
                for controller in sorted(ir.controllers, key=lambda value: value.id)
            ],
            "ignition_staging_path": "ignition/",
            "fat_plan_path": "tests/fat-plan.json",
            "qualified_runtime_evidence_required": True,
            "human_engineering_approval_required": True,
            "production_deployment_performed": False,
        }
        if _load_json(root / "engineering-handoff.json") != expected_handoff:
            errors.append("engineering-handoff.json does not match verified build intent")

        return {
            "status": "PASS" if not errors else "FAIL",
            "errors": errors,
            "project_id": spec.project_id,
            "spec_sha256": ir.spec_sha256,
            "controls_ir_sha256": controls_ir_sha256(ir),
        }
    except (ControlsBuildError, OSError, ValueError) as exc:
        return {"status": "FAIL", "errors": [str(exc)]}
