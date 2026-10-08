from __future__ import annotations

from pathlib import Path
from typing import Any

from devagent.controls.ir import ControlsIR
from devagent.controls.models import ControllerSpec
from devagent.controls.rockwell import render_rockwell_project
from devagent.plc.safe_analysis import analyze_rockwell_l5x


def verify_rockwell_roundtrip(
    ir: ControlsIR,
    controller: ControllerSpec,
    path: Path,
) -> dict[str, Any]:
    """Re-import generated L5X through the existing production PLC analyzer.

    This module is intentionally verification-only. It never invokes Studio
    5000, a controller connection, tag writes, forces, downloads, or mode changes.
    """

    expected = render_rockwell_project(ir, controller)
    engineering = analyze_rockwell_l5x(path)
    project = engineering.project

    actual_tags = sorted(
        item.name for item in project.tags if item.scope == "controller"
    )
    expected_tags = sorted(expected.tags)

    actual_rungs = [item.text for item in project.rungs]
    expected_rungs = [item.text for item in expected.rungs]

    expected_outputs = sorted(item.output_tag for item in expected.rungs)
    writer_counts = {
        output: sum(output in rung.writes for rung in project.rungs)
        for output in expected_outputs
    }
    bad_writer_counts = {
        output: count for output, count in writer_counts.items() if count != 1
    }

    mismatches: list[str] = []
    if project.metadata.controller_name != expected.controller_name:
        mismatches.append(
            f"controller name mismatch: {project.metadata.controller_name!r} != "
            f"{expected.controller_name!r}"
        )
    if actual_tags != expected_tags:
        missing = sorted(set(expected_tags) - set(actual_tags))
        extra = sorted(set(actual_tags) - set(expected_tags))
        mismatches.append(f"tag mismatch missing={missing} extra={extra}")
    if actual_rungs != expected_rungs:
        mismatches.append("generated rung sequence/text changed after L5X re-import")
    if project.unknown_instruction_names:
        mismatches.append(
            "unmodeled instruction(s): " + ", ".join(project.unknown_instruction_names)
        )
    if bad_writer_counts:
        mismatches.append(
            "generated output writer count must equal one: "
            + ", ".join(f"{name}={count}" for name, count in sorted(bad_writer_counts.items()))
        )
    if engineering.outcome.value != "STATICALLY_VERIFIED":
        mismatches.append(
            f"existing DevAgent PLC verifier outcome is {engineering.outcome.value}"
        )

    return {
        "schema": "devagent-controls-rockwell-roundtrip-v1",
        "controller_id": controller.id,
        "generated_sha256": expected.sha256,
        "reimported_sha256": project.metadata.source_sha256,
        "plc_outcome": engineering.outcome.value,
        "expected_tag_count": len(expected_tags),
        "actual_tag_count": len(actual_tags),
        "expected_rung_count": len(expected_rungs),
        "actual_rung_count": len(actual_rungs),
        "output_writer_counts": writer_counts,
        "unknown_instruction_names": list(project.unknown_instruction_names),
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
