from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devagent.controls.build import ControlsBuildError, verify_controls_build
from devagent.controls.ir import build_controls_ir, controls_ir_sha256
from devagent.controls.models import ControlSystemSpec
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.parser import load_control_system_spec
from devagent.controls.schema import parse_control_system_payload


def _json_changes(
    old: Any,
    new: Any,
    *,
    path: str = "$",
) -> list[dict[str, Any]]:
    if type(old) is not type(new):
        return [{"path": path, "kind": "CHANGED", "old": old, "new": new}]

    if isinstance(old, dict):
        changes: list[dict[str, Any]] = []
        keys = sorted(set(old) | set(new))
        for key in keys:
            child = f"{path}.{key}"
            if key not in old:
                changes.append({"path": child, "kind": "ADDED", "old": None, "new": new[key]})
            elif key not in new:
                changes.append({"path": child, "kind": "REMOVED", "old": old[key], "new": None})
            else:
                changes.extend(_json_changes(old[key], new[key], path=child))
        return changes

    if isinstance(old, list):
        changes: list[dict[str, Any]] = []
        maximum = max(len(old), len(new))
        for index in range(maximum):
            child = f"{path}[{index}]"
            if index >= len(old):
                changes.append({"path": child, "kind": "ADDED", "old": None, "new": new[index]})
            elif index >= len(new):
                changes.append({"path": child, "kind": "REMOVED", "old": old[index], "new": None})
            else:
                changes.extend(_json_changes(old[index], new[index], path=child))
        return changes

    if old != new:
        return [{"path": path, "kind": "CHANGED", "old": old, "new": new}]
    return []


def diff_control_specs(
    baseline: ControlSystemSpec,
    candidate: ControlSystemSpec,
) -> dict[str, Any]:
    baseline_payload = normalized_spec_payload(baseline)
    candidate_payload = normalized_spec_payload(candidate)
    baseline_ir = build_controls_ir(baseline)
    candidate_ir = build_controls_ir(candidate)
    changes = _json_changes(baseline_payload, candidate_payload)
    return {
        "schema": "devagent-controls-spec-diff-v1",
        "status": "PASS",
        "baseline_project_id": baseline.project_id,
        "candidate_project_id": candidate.project_id,
        "baseline_controls_ir_sha256": controls_ir_sha256(baseline_ir),
        "candidate_controls_ir_sha256": controls_ir_sha256(candidate_ir),
        "changed": bool(changes),
        "change_count": len(changes),
        "changes": changes,
    }


def diff_build_against_spec(
    baseline_build_dir: Path,
    candidate: ControlSystemSpec,
) -> dict[str, Any]:
    baseline_root = baseline_build_dir.expanduser().resolve(strict=True)
    verification = verify_controls_build(baseline_root)
    if verification["status"] != "PASS":
        raise ControlsBuildError(
            "baseline controls build is invalid: " + " | ".join(verification["errors"])
        )
    baseline_payload = json.loads(
        (baseline_root / "input" / "spec.json").read_text(encoding="utf-8")
    )
    baseline = parse_control_system_payload(baseline_payload)
    result = diff_control_specs(baseline, candidate)
    result["baseline_build_dir"] = str(baseline_root)
    return result


def diff_project_files(
    baseline_build_dir: Path,
    candidate_spec_path: Path,
) -> dict[str, Any]:
    return diff_build_against_spec(
        baseline_build_dir,
        load_control_system_spec(candidate_spec_path),
    )
