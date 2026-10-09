from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from devagent.controls.ir import ControlsIR
from devagent.controls.models import ControllerSpec
from devagent.controls.projection import compare_plc_projection
from devagent.controls.rockwell import render_rockwell_project
from devagent.plc.safe_analysis import analyze_rockwell_l5x


def verify_rockwell_roundtrip(
    ir: ControlsIR,
    controller: ControllerSpec,
    path: Path,
) -> dict[str, Any]:
    """Re-import generated L5X through the existing production PLC analyzer.

    Verification is deliberately independent of the XML writer after generation:
    Controls IR is projected into an expected canonical PLC semantic contract,
    the generated L5X is parsed by the existing PLC analyzer, and the two
    projections are compared. This module never invokes Studio 5000, a controller
    connection, tag writes, forces, downloads, or mode changes.
    """

    expected_artifact = render_rockwell_project(ir, controller)
    engineering = analyze_rockwell_l5x(path)
    project = engineering.project

    projection = compare_plc_projection(ir, controller, project)
    mismatches = list(projection["mismatches"])

    if project.unknown_instruction_names:
        mismatches.append(
            "unmodeled instruction(s): " + ", ".join(project.unknown_instruction_names)
        )
    if project.partially_modeled_instruction_names:
        mismatches.append(
            "partially modeled instruction(s): "
            + ", ".join(project.partially_modeled_instruction_names)
        )

    physical_writes = sorted(
        {
            written
            for rung in project.rungs
            for written in rung.writes
            if re.match(r"(?i)^(?:O:|Local:[^:]+:O\.)", written)
        }
    )
    if physical_writes:
        mismatches.append(
            "CTRL-E201 direct physical output write(s) are prohibited in generated "
            "standard logic: " + ", ".join(physical_writes)
        )

    if engineering.outcome.value != "STATICALLY_VERIFIED":
        mismatches.append(
            f"existing DevAgent PLC verifier outcome is {engineering.outcome.value}"
        )

    return {
        "schema": "devagent-controls-rockwell-roundtrip-v2",
        "controller_id": controller.id,
        "generated_sha256": expected_artifact.sha256,
        "reimported_sha256": project.metadata.source_sha256,
        "plc_outcome": engineering.outcome.value,
        "expected_projection_sha256": projection["expected_projection_sha256"],
        "expected_semantic_sha256": projection["expected_semantic_sha256"],
        "actual_semantic_sha256": projection["actual_semantic_sha256"],
        "semantic_projection_status": projection["status"],
        "output_writer_counts": projection["writer_counts"],
        "unknown_instruction_names": list(project.unknown_instruction_names),
        "partially_modeled_instruction_names": list(
            project.partially_modeled_instruction_names
        ),
        "direct_physical_output_writes": physical_writes,
        "mismatches": mismatches,
        "status": "PASS" if not mismatches else "FAIL",
        "authority": {
            "controller_connection": False,
            "tag_write": False,
            "force": False,
            "download": False,
            "mode_change": False,
        },
    }
