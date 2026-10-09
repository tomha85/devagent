from __future__ import annotations

import json

from devagent.entrypoint import main


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
                        "id": "VFD_101",
                        "type": "VFD",
                        "standard": "vfd-v1",
                        "controller": "PLC1",
                        "signals": ["DRIVE_READY", "DRIVE_FAULT"],
                        "commands": {"RUN": True, "STOP": True, "RESET": True},
                        "status": ["READY", "RUNNING", "FAULTED"],
                        "permissives": ["DRIVE_READY"],
                        "interlocks": ["DRIVE_FAULT"],
                        "faults": ["DRIVE_FAULT"],
                        "alarms": [
                            {
                                "id": "ALM_VFD101_FAULT",
                                "priority": "HIGH",
                                "operator_response": "Inspect drive fault.",
                                "source_signal": "DRIVE_FAULT",
                            }
                        ],
                        "hmi": {"faceplate": "vfd-v1", "historian": True},
                        "requirements": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_entrypoint_routes_controls_validate(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path)
    assert main(["controls", "validate", str(path)]) == 0
    output = capsys.readouterr().out
    assert "CONTROL_SPEC=PASS" in output
    assert "CONTROLS_IR_SHA256=" in output


def test_entrypoint_routes_controls_inspect(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path)
    assert main(["controls", "inspect", str(path)]) == 0
    output = capsys.readouterr().out
    assert '"devagent-controls-authoring-manifest-v2"' in output
    assert '"devagent-controls-ir-v2"' in output


def test_entrypoint_routes_controls_build_verify_and_review(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path)
    build = tmp_path / "build"
    review = tmp_path / "review-request.json"

    assert main(
        ["controls", "build", str(path), "--output-dir", str(build)]
    ) == 0
    built = capsys.readouterr().out
    assert "CONTROLS_BUILD=PASS" in built
    assert "READINESS=READY_FOR_ENGINEERING_REVIEW" in built

    assert main(["controls", "verify", str(build)]) == 0
    verified = capsys.readouterr().out
    assert "CONTROLS_BUILD_VERIFY=PASS" in verified

    assert main(
        [
            "controls",
            "request-review",
            str(build),
            "--requested-by",
            "Lead Controls Engineer",
            "--output",
            str(review),
        ]
    ) == 0
    requested = capsys.readouterr().out
    assert "CONTROLS_REVIEW_REQUEST=PASS" in requested
    assert review.is_file()


def test_entrypoint_routes_controls_catalog(capsys) -> None:
    assert main(["controls", "catalog"]) == 0
    output = capsys.readouterr().out
    assert '"devagent-controls-catalog-v2"' in output
    assert '"conveyor-v1"' in output
    assert '"motor-v1"' in output


def test_entrypoint_routes_controls_diff(tmp_path, capsys) -> None:
    baseline_spec = _write_spec(tmp_path)
    baseline_build = tmp_path / "baseline-build"
    assert main(
        ["controls", "build", str(baseline_spec), "--output-dir", str(baseline_build)]
    ) == 0
    capsys.readouterr()

    candidate_payload = json.loads(baseline_spec.read_text(encoding="utf-8"))
    candidate_payload["equipment"][0]["hmi"]["historian"] = False
    candidate_spec = tmp_path / "candidate.json"
    candidate_spec.write_text(json.dumps(candidate_payload), encoding="utf-8")

    assert main(
        ["controls", "diff", str(baseline_build), str(candidate_spec)]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "devagent-controls-spec-diff-v1"
    assert output["changed"] is True
    assert output["change_count"] >= 1
