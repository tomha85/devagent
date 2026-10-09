from __future__ import annotations

import ast
from pathlib import Path


def test_controls_has_no_live_or_control_runtime_dependency() -> None:
    root = Path(__file__).resolve().parents[1] / "devagent" / "controls"
    assert root.is_dir()

    forbidden_prefixes = (
        "devagent.live",
        "subprocess",
        "socket",
        "ctypes",
        "asyncio.subprocess",
    )
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)

            for name in imported:
                assert not name.startswith(forbidden_prefixes), (
                    f"{path.name} crosses Controls authority boundary via {name}"
                )
                if name.startswith("devagent.plc"):
                    assert path.name == "verification.py"
                    assert name == "devagent.plc.safe_analysis"


def test_controls_manifest_declares_no_control_authority() -> None:
    from devagent.controls.ir import build_controls_ir
    from devagent.controls.manifest import build_authoring_manifest
    from devagent.controls.schema import parse_control_system_payload

    spec = parse_control_system_payload(
        {
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
