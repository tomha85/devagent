from __future__ import annotations

from devagent.controls.portal import PortalService


def _payload():
    return {
        "schema": "devagent-controls-spec-v2",
        "project_id": "CELL01",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
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
    review = service.request_review_payload(_payload(), requested_by="Lead Controls Engineer")

    assert validation["status"] == "PASS"
    assert first["status"] == "PASS"
    assert first["verification"]["status"] == "PASS"
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["build_dir"] == first["build_dir"]
    assert review["status"] == "PASS"
    assert review["request"]["requested_action"] == "ENGINEERING_REVIEW"
    assert review["request"]["production_release_ready"] is False
    assert review["request"]["human_engineering_approval_required"] is True


def test_portal_uses_portable_filesystem_identity(tmp_path) -> None:
    payload = _payload()
    payload["project_id"] = "CELL:01"
    service = PortalService(tmp_path / "portal")

    result = service.build_payload(payload)

    assert result["status"] == "PASS"
    assert ":" not in result["build_dir"].split("/")[-1]
    assert "CELL_01" in result["build_dir"].split("/")[-1]


def test_portal_exposes_same_versioned_catalog(tmp_path) -> None:
    service = PortalService(tmp_path / "portal")
    catalog = service.catalog_payload()

    assert catalog["schema"] == "devagent-controls-catalog-v2"
    ids = {item["id"] for item in catalog["standards"]}
    assert ids == {"motor-v1", "vfd-v1", "conveyor-v1", "valve-v1"}
