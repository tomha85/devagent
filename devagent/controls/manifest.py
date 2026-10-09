from __future__ import annotations

from typing import Any

from devagent.controls.ir import ControlsIR, controls_ir_sha256
from devagent.controls.models import ControlSystemSpec

AUTHORING_MANIFEST_SCHEMA = "devagent-controls-authoring-manifest-v2"


def build_authoring_manifest(spec: ControlSystemSpec, ir: ControlsIR) -> dict[str, Any]:
    return {
        "schema": AUTHORING_MANIFEST_SCHEMA,
        "project_id": spec.project_id,
        "spec_schema": spec.schema,
        "ir_schema": ir.schema,
        "spec_sha256": ir.spec_sha256,
        "controls_ir_sha256": controls_ir_sha256(ir),
        "controller_count": len(spec.controllers),
        "equipment_count": len(spec.equipment),
        "authority": {
            "plc_write": False,
            "plc_force": False,
            "plc_download": False,
            "plc_mode_change": False,
            "live_control": False,
        },
    }
