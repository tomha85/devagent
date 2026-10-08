from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from devagent.controls.build import ControlsBuildError, verify_controls_build


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_review_request(
    build_dir: Path,
    *,
    requested_by: str,
    output_path: Path,
) -> dict[str, Any]:
    build = build_dir.expanduser().resolve(strict=True)
    identity = requested_by.strip()
    if not identity:
        raise ControlsBuildError("requested_by must not be empty")

    verification = verify_controls_build(build)
    if verification["status"] != "PASS":
        raise ControlsBuildError(
            "cannot request engineering review for an invalid build: "
            + " | ".join(verification["errors"])
        )

    manifest_path = build / "generation-manifest.json"
    readiness_path = build / "release-readiness.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    if readiness.get("status") != "READY_FOR_ENGINEERING_REVIEW":
        raise ControlsBuildError(
            f"build readiness is {readiness.get('status')!r}, not READY_FOR_ENGINEERING_REVIEW"
        )

    request = {
        "schema": "devagent-controls-engineering-review-request-v1",
        "project_id": manifest["project_id"],
        "spec_sha256": manifest["spec_sha256"],
        "controls_ir_sha256": manifest["controls_ir_sha256"],
        "generation_manifest_sha256": _sha256(manifest_path),
        "requested_by": identity,
        "requested_action": "ENGINEERING_REVIEW",
        "human_engineering_approval_required": True,
        "production_release_ready": False,
        "production_deployment_requested": False,
        "external_qualification": manifest["external_qualification"],
        "instructions": (
            "Review generated PLC/HMI staging artifacts and verification evidence. "
            "Qualified vendor/runtime FAT evidence and a separate approved release "
            "decision are required before production deployment."
        ),
    }
    target = output_path.expanduser().resolve(strict=False)
    if target.exists():
        raise ControlsBuildError(f"review request output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return request
