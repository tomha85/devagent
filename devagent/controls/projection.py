from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from devagent.controls.ir import ControlsIR
from devagent.controls.models import ControllerSpec
from devagent.controls.rockwell import generated_rungs
from devagent.controls.symbols import controller_symbol, equipment_symbol_map
from devagent.plc.models import CanonicalPLCProject

_CONTACT_RE = re.compile(r"\b(?:XIC|XIO)\(([^)]+)\)")


def _normalize_rung_text(value: str) -> str:
    # Studio 5000 may rewrite insignificant whitespace on import/export.
    # Rung order, instructions, operands, reads, and writes remain authoritative.
    return re.sub(r"\s+", "", value or "")


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def expected_plc_projection(ir: ControlsIR, controller: ControllerSpec) -> dict[str, Any]:
    equipment = tuple(
        item for item in ir.equipment if item.controller == controller.id
    )
    tags: list[dict[str, Any]] = []
    rungs: list[dict[str, Any]] = []

    for item in equipment:
        symbols = equipment_symbol_map(item)
        for category in ("signals", "commands", "status", "outputs", "internal"):
            for logical_name, tag_name in sorted(dict(symbols[category]).items()):
                logical_kind = {
                    "signals": "SIGNAL",
                    "commands": "COMMAND",
                    "status": "STATUS",
                    "outputs": "OUTPUT",
                    "internal": "INTERNAL",
                }[category]
                tags.append(
                    {
                        "name": str(tag_name),
                        "data_type": "BOOL",
                        "external_access": (
                            "Read/Write"
                            if category == "commands"
                            else "None"
                            if category == "internal"
                            else "Read Only"
                        ),
                        "equipment_id": item.id,
                        "logical_ref": f"{logical_kind}.{logical_name}",
                    }
                )

        for generated in generated_rungs(item):
            reads = tuple(sorted(set(_CONTACT_RE.findall(generated.text))))
            rungs.append(
                {
                    "equipment_id": generated.equipment_id,
                    "purpose": generated.purpose,
                    "text": _normalize_rung_text(generated.text),
                    "reads": list(reads),
                    "writes": [generated.output_tag],
                }
            )

    tags.sort(key=lambda value: value["name"])
    return {
        "schema": "devagent-controls-expected-plc-projection-v1",
        "controller_id": controller.id,
        "controller_name": controller_symbol(controller.id),
        "vendor": controller.vendor,
        "platform": controller.platform,
        "program": "DevAgentGenerated",
        "routine": "MainLogic",
        "tags": tags,
        "rungs": rungs,
    }


def actual_plc_projection(
    project: CanonicalPLCProject,
    expected: dict[str, Any],
) -> dict[str, Any]:
    expected_tag_names = {item["name"] for item in expected["tags"]}
    tags = [
        {
            "name": tag.name,
            "data_type": tag.data_type,
            "external_access": tag.external_access,
        }
        for tag in project.tags
        if tag.scope == "controller" and tag.name in expected_tag_names
    ]
    tags.sort(key=lambda value: value["name"])

    generated_rungs = [
        rung
        for rung in project.rungs
        if rung.program == expected["program"] and rung.routine == expected["routine"]
    ]
    rungs = [
        {
            "text": _normalize_rung_text(rung.text),
            "reads": list(sorted(set(rung.reads))),
            "writes": list(sorted(set(rung.writes))),
        }
        for rung in generated_rungs
    ]
    return {
        "schema": "devagent-controls-actual-plc-projection-v1",
        "controller_name": project.metadata.controller_name,
        "vendor": project.metadata.vendor,
        "processor_type": project.metadata.processor_type,
        "tags": tags,
        "rungs": rungs,
        "all_controller_tag_names": sorted(
            tag.name for tag in project.tags if tag.scope == "controller"
        ),
        "all_generated_rung_count": len(generated_rungs),
    }


def compare_plc_projection(
    ir: ControlsIR,
    controller: ControllerSpec,
    project: CanonicalPLCProject,
) -> dict[str, Any]:
    expected = expected_plc_projection(ir, controller)
    actual = actual_plc_projection(project, expected)
    mismatches: list[str] = []

    if actual["controller_name"] != expected["controller_name"]:
        mismatches.append(
            f"controller name mismatch: {actual['controller_name']!r} != "
            f"{expected['controller_name']!r}"
        )

    expected_tags = {
        item["name"]: {
            "data_type": item["data_type"],
            "external_access": item["external_access"],
        }
        for item in expected["tags"]
    }
    actual_tags = {
        item["name"]: {
            "data_type": item["data_type"],
            "external_access": item["external_access"],
        }
        for item in actual["tags"]
    }
    all_actual_names = set(actual["all_controller_tag_names"])
    if set(expected_tags) != all_actual_names:
        mismatches.append(
            "controller tag set mismatch missing="
            + repr(sorted(set(expected_tags) - all_actual_names))
            + " extra="
            + repr(sorted(all_actual_names - set(expected_tags)))
        )
    for name, expected_contract in sorted(expected_tags.items()):
        if name not in actual_tags:
            continue
        if actual_tags[name] != expected_contract:
            mismatches.append(
                f"tag contract mismatch {name}: "
                f"{actual_tags[name]!r} != {expected_contract!r}"
            )

    expected_rungs = [
        {
            "text": item["text"],
            "reads": list(sorted(item["reads"])),
            "writes": list(sorted(item["writes"])),
        }
        for item in expected["rungs"]
    ]
    if actual["rungs"] != expected_rungs:
        mismatches.append("generated program semantic rung projection differs from Controls IR")

    writer_counts: dict[str, int] = {}
    for item in actual["rungs"]:
        for written in item["writes"]:
            writer_counts[written] = writer_counts.get(written, 0) + 1
    expected_outputs = {
        written
        for item in expected_rungs
        for written in item["writes"]
    }
    bad_writer_counts = {
        output: writer_counts.get(output, 0)
        for output in sorted(expected_outputs)
        if writer_counts.get(output, 0) != 1
    }
    if bad_writer_counts:
        mismatches.append(
            "generated output writer count must equal one: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(bad_writer_counts.items())
            )
        )

    expected_hash = _sha256(expected)
    actual_semantic = {
        "controller_name": actual["controller_name"],
        "tags": actual_tags,
        "rungs": actual["rungs"],
    }
    expected_semantic = {
        "controller_name": expected["controller_name"],
        "tags": expected_tags,
        "rungs": expected_rungs,
    }
    return {
        "schema": "devagent-controls-plc-projection-comparison-v1",
        "status": "PASS" if not mismatches else "FAIL",
        "controller_id": controller.id,
        "expected_projection_sha256": expected_hash,
        "expected_semantic_sha256": _sha256(expected_semantic),
        "actual_semantic_sha256": _sha256(actual_semantic),
        "writer_counts": writer_counts,
        "mismatches": mismatches,
        "expected": expected,
        "actual": actual,
    }
