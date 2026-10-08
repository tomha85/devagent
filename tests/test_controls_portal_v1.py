from __future__ import annotations

from devagent.controls.portal import PortalService


def _payload():
    return {
        "schema": "devagent-controls-spec-v1",
        "project_id": "CELL01",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "COMPACTLOGIX"}
        ],
        "equipment": [
            {
                "id": "MTR_101",
                "type": "MOTOR",
                "standard": "motor-v1",
                "controller": "PLC1",
                "signals": ["SAFETY_OK", "OVERLOAD"],
                "commands": {"START": True, "STOP": True, "RESET": True},
                "status": ["READY", "RUNNING", "FAULTED"],
                "permissives": ["SAFETY_OK"],
                "interlocks": ["OVERLOAD"],
                "alarms": [
                    {
                        "id": "ALM_OVERLOAD",
                        "priority": "HIGH",
                        "operator_response": "Inspect overload.",
                        "source_signal": "OVERLOAD",
                    }
                ],
                "hmi": {"faceplate": "motor-v1", "historian": False},
                "requirements": [],
            }
        ],
    }


def test_portal_service_reuses_same_deterministic_library(tmp_path) -> None:
    service = PortalService(tmp_path / "portal")
    validation = service.validate_payload(_payload())
    first = service.build_payload(_payload())
    second = service.build_payload(_payload())

    assert validation["status"] == "PASS"
    assert first["status"] == "PASS"
    assert first["verification"]["status"] == "PASS"
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["build_dir"] == first["build_dir"]
