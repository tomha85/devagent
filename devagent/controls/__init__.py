"""Deterministic controls engineering authoring foundation.

Keep package import lightweight: generation/verification/portal modules are
opt-in so importing the schema never pulls PLC analysis or server surfaces.
"""

from devagent.controls.ir import ControlsIR, build_controls_ir, controls_ir_sha256
from devagent.controls.parser import load_control_system_spec
from devagent.controls.schema import (
    CONTROL_SPEC_SCHEMA,
    ControlSpecError,
    parse_control_system_payload,
)

__all__ = [
    "CONTROL_SPEC_SCHEMA",
    "ControlSpecError",
    "ControlsIR",
    "build_controls_ir",
    "controls_ir_sha256",
    "load_control_system_spec",
    "parse_control_system_payload",
]
