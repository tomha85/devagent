"""Deterministic controls-authoring foundation for DevAgent.

This package intentionally owns specification parsing and authoring intent only.
It does not write to PLCs, invoke vendor engineering software, or alter DevAgent
PLC/Live authority boundaries.
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
