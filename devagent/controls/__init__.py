"""Deterministic company-wide controls engineering platform.

The package owns specification, generation, staging verification, FAT planning,
and self-service authoring. Production PLC/HMI deployment remains outside this
authority boundary.
"""

from devagent.controls.build import (
    ControlsBuildError,
    ControlsBuildResult,
    build_controls_project,
    build_controls_spec,
    verify_controls_build,
)
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
    "ControlsBuildError",
    "ControlsBuildResult",
    "ControlsIR",
    "build_controls_ir",
    "build_controls_project",
    "build_controls_spec",
    "controls_ir_sha256",
    "load_control_system_spec",
    "parse_control_system_payload",
    "verify_controls_build",
]
