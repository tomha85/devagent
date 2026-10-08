from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from devagent.controls.models import ControlSystemSpec, ControllerSpec, EquipmentSpec
from devagent.controls.normalize import (
    canonical_json_bytes,
    canonical_spec,
    normalized_spec_payload,
    spec_sha256,
)

CONTROLS_IR_SCHEMA = "devagent-controls-ir-v1"


@dataclass(frozen=True)
class ControlsIR:
    """Deeply immutable deterministic authoring intermediate representation."""

    schema: str
    spec_schema: str
    project_id: str
    spec_sha256: str
    controllers: tuple[ControllerSpec, ...]
    equipment: tuple[EquipmentSpec, ...]


def build_controls_ir(spec: ControlSystemSpec) -> ControlsIR:
    canonical = canonical_spec(spec)
    return ControlsIR(
        schema=CONTROLS_IR_SCHEMA,
        spec_schema=canonical.schema,
        project_id=canonical.project_id,
        spec_sha256=spec_sha256(canonical),
        controllers=canonical.controllers,
        equipment=canonical.equipment,
    )


def controls_ir_payload(ir: ControlsIR) -> dict[str, Any]:
    canonical = ControlSystemSpec(
        schema=ir.spec_schema,
        project_id=ir.project_id,
        controllers=ir.controllers,
        equipment=ir.equipment,
    )
    normalized = normalized_spec_payload(canonical)
    return {
        "schema": ir.schema,
        "spec_schema": ir.spec_schema,
        "project_id": ir.project_id,
        "spec_sha256": ir.spec_sha256,
        "controllers": normalized["controllers"],
        "equipment": normalized["equipment"],
    }


def controls_ir_sha256(ir: ControlsIR) -> str:
    return hashlib.sha256(canonical_json_bytes(controls_ir_payload(ir))).hexdigest()
