from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from devagent.controls.models import ControlSystemSpec
from devagent.controls.normalize import canonical_json_bytes, normalized_spec_payload, spec_sha256

CONTROLS_IR_SCHEMA = "devagent-controls-ir-v1"


@dataclass(frozen=True)
class ControlsIR:
    schema: str
    spec_schema: str
    project_id: str
    spec_sha256: str
    controllers: tuple[dict[str, Any], ...]
    equipment: tuple[dict[str, Any], ...]


def build_controls_ir(spec: ControlSystemSpec) -> ControlsIR:
    normalized = normalized_spec_payload(spec)
    return ControlsIR(
        schema=CONTROLS_IR_SCHEMA,
        spec_schema=spec.schema,
        project_id=spec.project_id,
        spec_sha256=spec_sha256(spec),
        controllers=tuple(normalized["controllers"]),
        equipment=tuple(normalized["equipment"]),
    )


def controls_ir_payload(ir: ControlsIR) -> dict[str, Any]:
    return {
        "schema": ir.schema,
        "spec_schema": ir.spec_schema,
        "project_id": ir.project_id,
        "spec_sha256": ir.spec_sha256,
        "controllers": list(ir.controllers),
        "equipment": list(ir.equipment),
    }


def controls_ir_sha256(ir: ControlsIR) -> str:
    return hashlib.sha256(canonical_json_bytes(controls_ir_payload(ir))).hexdigest()
