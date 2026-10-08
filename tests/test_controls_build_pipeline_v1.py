from __future__ import annotations

import json

from devagent.controls.build import build_controls_project, verify_controls_build


def _write_spec(tmp_path):
    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-controls-spec-v1",
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
                        "signals": ["SAFE", "GUARD_OPEN", "DRIVE_FAULT"],
                        "commands": {"START": True, "STOP": True, "RESET": True},
                        "status": ["READY", "RUNNING", "FAULTED"],
                        "permissives": ["SAFE"],
                        "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                        "alarms": [
                            {
                                "id": "ALM_DRIVE",
                                "priority": "HIGH",
                                "operator_response": "Inspect drive.",
                                "source_signal": "DRIVE_FAULT",
                            }
                        ],
                        "hmi": {"faceplate": "conveyor-v1", "historian": True},
                        "requirements": [],
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
