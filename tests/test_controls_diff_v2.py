from __future__ import annotations

from devagent.controls.build import build_controls_spec
from devagent.controls.diff import diff_build_against_spec
from devagent.controls.portal import PortalService
from devagent.controls.schema import parse_control_system_payload


def _payload(*, historian: bool) -> dict:
    return {
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
                        "description": "Equipment alarm.",
                        "on_delay_ms": 0,
                        "operator_response": "Inspect drive fault.",
                        "source_signal": "DRIVE_FAULT",
                    }
                ],
                "hmi": {"faceplate": "conveyor-v1", "historian": historian},
                "requirements": [],
            }
        ],
    }


def test_verified_build_diff_reports_engineering_change(tmp_path) -> None:
    baseline = parse_control_system_payload(_payload(historian=False))
    candidate = parse_control_system_payload(_payload(historian=True))
    build = tmp_path / "baseline"
    result = build_controls_spec(baseline, build)

    diff = diff_build_against_spec(build, candidate)

    assert diff["status"] == "PASS"
    assert diff["changed"] is True
    assert diff["baseline_controls_ir_sha256"] == result.manifest["controls_ir_sha256"]
    assert diff["candidate_controls_ir_sha256"] != diff["baseline_controls_ir_sha256"]
    assert any(
        item["path"] == "$.equipment[0].hmi.historian"
        and item["old"] is False
        and item["new"] is True
        for item in diff["changes"]
    )


def test_portal_diff_requires_exact_verified_workspace_baseline(tmp_path) -> None:
    service = PortalService(tmp_path / "workspace")
    baseline_result = service.build_payload(_payload(historian=False))
    baseline_hash = baseline_result["verification"]["controls_ir_sha256"]

    diff = service.diff_payload(
        _payload(historian=True),
        baseline_controls_ir_sha256=baseline_hash,
    )

    assert diff["changed"] is True
    assert diff["baseline_controls_ir_sha256"] == baseline_hash
