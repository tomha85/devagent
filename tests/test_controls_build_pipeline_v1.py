from __future__ import annotations

import hashlib
import json

import pytest

from devagent.controls.build import (
    ControlsBuildError,
    build_controls_project,
    build_controls_spec,
    verify_controls_build,
)
from devagent.controls.schema import parse_control_system_payload
from devagent.plc.requirements import ingest_requirements


def _write_spec(tmp_path):
    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-controls-spec-v2",
                "project_id": "PACK01",
                "controllers": [
                    {"id": "PLC1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
                ],
                "equipment": [
                    {
                        "id": "CONV_101",
                        "type": "CONVEYOR",
                        "standard": "conveyor-v1",
                        "controller": "PLC1",
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
                                "operator_response": "Inspect drive.",
                                "source_signal": "DRIVE_FAULT",
                            }
                        ],
                        "hmi": {"faceplate": "conveyor-v1", "historian": True},
                        "requirements": [
                            {
                                "id": "REQ_CONV101_GUARD",
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
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_build_is_end_to_end_self_verified_and_never_claims_runtime_release(tmp_path) -> None:
    spec = _write_spec(tmp_path)
    output = tmp_path / "build"

    result = build_controls_project(spec, output)
    verification = verify_controls_build(output)
    readiness = json.loads((output / "release-readiness.json").read_text(encoding="utf-8"))

    assert result.status == "PASS"
    assert verification["status"] == "PASS"
    assert (output / "rockwell" / "PLC1.L5X").is_file()
    assert (output / "ignition" / "equipment.json").is_file()
    assert (output / "tests" / "fat-plan.json").is_file()
    requirements_path = output / "requirements" / "controls-requirements.json"
    assert requirements_path.is_file()
    requirements = ingest_requirements([requirements_path])
    assert [item.id for item in requirements] == ["REQ_CONV101_GUARD"]
    assert requirements[0].criticality.value == "HIGH"
    handoff = json.loads((output / "engineering-handoff.json").read_text(encoding="utf-8"))
    assert handoff["plc_review"][0]["requirements_path"] == "requirements/by-controller/PLC1.json"
    assert "--requirements requirements/by-controller/PLC1.json" in handoff["plc_review"][0]["next_command"]
    assert readiness["status"] == "READY_FOR_ENGINEERING_REVIEW"
    assert readiness["fat_execution"] == "NOT_RUN"
    assert readiness["production_release_ready"] is False
    assert readiness["human_engineering_approval_required"] is True


def test_two_builds_have_identical_manifest_artifact_hashes(tmp_path) -> None:
    spec = _write_spec(tmp_path)
    first = build_controls_project(spec, tmp_path / "build-a")
    second = build_controls_project(spec, tmp_path / "build-b")

    assert first.manifest["spec_sha256"] == second.manifest["spec_sha256"]
    assert first.manifest["controls_ir_sha256"] == second.manifest["controls_ir_sha256"]
    assert first.manifest["artifact_sha256"] == second.manifest["artifact_sha256"]


def test_verifier_detects_post_build_tampering(tmp_path) -> None:
    spec = _write_spec(tmp_path)
    output = tmp_path / "build"
    build_controls_project(spec, output)

    project = output / "rockwell" / "PLC1.L5X"
    project.write_text(project.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    verification = verify_controls_build(output)
    assert verification["status"] == "FAIL"
    assert any("hash mismatch" in item for item in verification["errors"])


def test_controller_names_that_collide_after_rockwell_normalization_fail_closed(tmp_path) -> None:
    payload = {
        "schema": "devagent-controls-spec-v2",
        "project_id": "COLLISION_TEST",
        "controllers": [
            {"id": "PLC-1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"},
            {"id": "PLC_1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"},
        ],
        "equipment": [],
    }
    base = json.loads(_write_spec(tmp_path).read_text(encoding="utf-8"))["equipment"][0]
    first = dict(base)
    first["id"] = "CONV_A"
    first["controller"] = "PLC-1"
    first["alarms"] = []
    first["requirements"] = []
    second = json.loads(json.dumps(first))
    second["id"] = "CONV_B"
    second["controller"] = "PLC_1"
    payload["equipment"] = [first, second]

    spec = parse_control_system_payload(payload)
    with pytest.raises(ControlsBuildError, match="controller identities collide"):
        build_controls_spec(spec, tmp_path / "collision-build")


def _refresh_manifest_hash(output, relative_path: str) -> None:
    manifest_path = output / "generation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = output / relative_path
    manifest["artifact_sha256"][relative_path] = hashlib.sha256(payload.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_verifier_rederives_requirements_even_if_manifest_hash_is_updated(tmp_path) -> None:
    spec = _write_spec(tmp_path)
    output = tmp_path / "build"
    build_controls_project(spec, output)

    relative = "requirements/controls-requirements.json"
    path = output / relative
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["requirements"][0]["text"] = "Tampered requirement."
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_manifest_hash(output, relative)

    verification = verify_controls_build(output)
    assert verification["status"] == "FAIL"
    assert any("requirements handoff" in item for item in verification["errors"])


def test_verifier_rejects_release_claim_even_if_manifest_hash_is_updated(tmp_path) -> None:
    spec = _write_spec(tmp_path)
    output = tmp_path / "build"
    build_controls_project(spec, output)

    relative = "release-readiness.json"
    path = output / relative
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["production_release_ready"] = True
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_manifest_hash(output, relative)

    verification = verify_controls_build(output)
    assert verification["status"] == "FAIL"
    assert any("production release readiness" in item for item in verification["errors"])
