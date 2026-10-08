from __future__ import annotations

import json

from devagent.entrypoint import main


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
                        "id": "VFD_101",
                        "type": "VFD",
                        "standard": "vfd-v1",
                        "controller": "PLC1",
                        "signals": ["DRIVE_READY", "DRIVE_FAULT"],
                        "commands": {"RUN": True, "STOP": True, "RESET": True},
                        "status": ["READY", "RUNNING", "FAULTED"],
                        "permissives": ["DRIVE_READY"],
                        "interlocks": ["DRIVE_FAULT"],
                        "alarms": [],
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
    assert '"devagent-controls-authoring-manifest-v1"' in output
    assert '"devagent-controls-ir-v1"' in output
