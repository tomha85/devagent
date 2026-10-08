from __future__ import annotations

import ast
from pathlib import Path


def test_controls_authoring_has_no_runtime_or_control_dependency() -> None:
    root = Path(__file__).resolve().parents[1] / "devagent" / "controls"
    assert root.is_dir()

    forbidden_prefixes = (
        "devagent.live",
        "devagent.plc",
        "subprocess",
        "socket",
        "ctypes",
        "asyncio.subprocess",
    )
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"{path.name} crosses authoring authority boundary via {node.module}"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"{path.name} crosses authoring authority boundary via {alias.name}"
                    )


def test_controls_manifest_declares_no_control_authority() -> None:
    from devagent.controls.ir import build_controls_ir
    from devagent.controls.manifest import build_authoring_manifest
    from devagent.controls.schema import parse_control_system_payload

    spec = parse_control_system_payload(
        {
            "schema": "devagent-controls-spec-v1",
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
                    "signals": ["SAFE"],
                    "commands": {"START": True},
                    "status": ["READY"],
                    "permissives": ["SAFE"],
                    "interlocks": [],
                    "alarms": [],
                    "hmi": {"faceplate": "motor-v1", "historian": False},
                    "requirements": [],
                }
            ],
        }
    )
    manifest = build_authoring_manifest(spec, build_controls_ir(spec))
    assert manifest["authority"] == {
        "plc_write": False,
        "plc_force": False,
        "plc_download": False,
        "plc_mode_change": False,
        "live_control": False,
    }
