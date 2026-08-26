#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.rockwell_closeout import rockwell_capability_profile
from devagent.plc.rockwell_l5x import L5XError

_UPSTREAM_COMMIT = "de14d4ac87d5295b2380cce441ed581d1930947f"
_BASE = (
    "https://raw.githubusercontent.com/RockwellAutomation/ra-logix-cicd/"
    + _UPSTREAM_COMMIT
    + "/1-production-files/L5Xs/"
)
_FULL_PROJECT = {
    "name": "ExampleForCICD_L85E.L5X",
    "url": _BASE + "ExampleForCICD_L85E.L5X",
    "git_blob_sha1": "ea3814f7d3657de569539228042903dc9ea8a908",
    "size": 20397,
}
_COMPONENT_EXPORT = {
    "name": "DelayedSum_AOI.L5X",
    "url": _BASE + "DelayedSum_AOI.L5X",
    "git_blob_sha1": "be230f7e191894efe42d22db37e648572752bf99",
    "size": 4549,
}
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOST = "raw.githubusercontent.com"


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()  # nosec B324 - Git object identity, not security auth


def _validate_download_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != _ALLOWED_DOWNLOAD_HOST:
        raise RuntimeError("Rockwell qualification artifact URL must use pinned raw.githubusercontent.com HTTPS")


def _download(spec: dict[str, object], directory: Path) -> tuple[Path, str]:
    url = str(spec["url"])
    _validate_download_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "devagent-rockwell-v9-qualification"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - host and scheme validated above
        final_url = response.geturl()
        _validate_download_url(final_url)
        payload = response.read(_MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > _MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"{spec['name']} exceeds qualification download limit")
    expected_size = int(spec["size"])
    if len(payload) != expected_size:
        raise RuntimeError(
            f"Pinned Rockwell artifact size mismatch for {spec['name']}: "
            f"expected {expected_size}, got {len(payload)}"
        )
    actual_blob = _git_blob_sha1(payload)
    if actual_blob != spec["git_blob_sha1"]:
        raise RuntimeError(
            f"Pinned Rockwell artifact identity mismatch for {spec['name']}: "
            f"expected {spec['git_blob_sha1']}, got {actual_blob}"
        )
    target = directory / str(spec["name"])
    target.write_bytes(payload)
    return target, hashlib.sha256(payload).hexdigest()


def _qualify_full_project(path: Path, source_sha256: str) -> dict[str, object]:
    result = run_production_verification_v5(path)
    engineering = result.engineering
    project = engineering.project
    profile = rockwell_capability_profile(project)

    expected_inventory = {
        "tags": 33,
        "programs": 4,
        "routines": 6,
        "rungs": 13,
        "instructions": 49,
        "branched_rungs": 8,
    }
    actual_inventory = {
        "tags": len(project.tags),
        "programs": len(project.programs),
        "routines": len(project.routines),
        "rungs": len(project.rungs),
        "instructions": project.instruction_total,
        "branched_rungs": project.branch_rung_total,
    }
    if actual_inventory != expected_inventory:
        raise RuntimeError(
            "Official Rockwell L85E inventory drifted: "
            f"expected {expected_inventory}, got {actual_inventory}"
        )

    if project.metadata.controller_name != "ExampleWithCICD_L85E":
        raise RuntimeError(f"Unexpected controller name: {project.metadata.controller_name}")
    if project.metadata.processor_type != "1756-L85E":
        raise RuntimeError(f"Unexpected processor type: {project.metadata.processor_type}")
    if project.metadata.software_revision != "36.00":
        raise RuntimeError(f"Unexpected Studio 5000 revision: {project.metadata.software_revision}")
    if not project.metadata.full_project:
        raise RuntimeError("Official Rockwell qualification artifact was not accepted as a full project")
    if project.metadata.source_sha256 != source_sha256:
        raise RuntimeError("Analyzer source SHA-256 does not match downloaded official artifact")

    # Instruction-level directional semantics are a hard regression gate for
    # this official sample. Branch-path proofs are intentionally narrower: the
    # sample contains mixed-action branches (GSV/MOVE/CLR/SSV/OTE) that are not
    # part of DevAgent's bounded Boolean branch theorem. Those branches must be
    # reported as explicit fail-closed gaps rather than promoted to VERIFIED.
    if project.instruction_semantic_count != project.instruction_total:
        raise RuntimeError(
            "Official L85E instruction semantic coverage regressed: "
            f"{project.instruction_semantic_count}/{project.instruction_total}"
        )
    if not 0 < project.branch_rung_semantic_count <= project.branch_rung_total:
        raise RuntimeError(
            "Official L85E branch semantic accounting is invalid: "
            f"{project.branch_rung_semantic_count}/{project.branch_rung_total}"
        )

    unmodeled_branches = project.branch_rung_total - project.branch_rung_semantic_count
    if profile["static_gaps"].get("unmodeled_branches") != unmodeled_branches:
        raise RuntimeError(
            "Official L85E capability profile branch gap disagrees with canonical IR: "
            f"expected {unmodeled_branches}, got {profile['static_gaps'].get('unmodeled_branches')}"
        )

    if unmodeled_branches:
        if profile["static_contract"] != "PARTIAL_FAIL_CLOSED":
            raise RuntimeError(
                "Official L85E contains unmodeled branches and must remain PARTIAL_FAIL_CLOSED; got "
                f"{profile['static_contract']}"
            )
        if engineering.outcome.value != "PARTIALLY_VERIFIED":
            raise RuntimeError(
                "Official L85E unmodeled branches must withhold full static verification; got "
                f"{engineering.outcome.value}"
            )
    else:
        if profile["static_contract"] != "COMPLETE":
            raise RuntimeError(
                "Official L85E has no static gaps but support contract is not COMPLETE: "
                + json.dumps(profile["static_gaps"], sort_keys=True)
            )
        if engineering.outcome.value != "STATICALLY_VERIFIED":
            raise RuntimeError(f"Official L85E static outcome regressed to {engineering.outcome.value}")

    if len(engineering.fat_tests) < 6:
        raise RuntimeError(
            f"Official L85E generated only {len(engineering.fat_tests)} FAT candidates; expected at least 6"
        )
    if any(test.execution_status != "NOT_RUN" for test in engineering.fat_tests):
        raise RuntimeError("Static qualification must never mark a FAT candidate as executed")
    if result.executions:
        raise RuntimeError("Static official qualification unexpectedly contains runtime execution evidence")
    if len(result.stages) != 15:
        raise RuntimeError(f"Production pipeline stage count regressed: {len(result.stages)}")
    if result.readiness is None:
        raise RuntimeError("Production pipeline did not evaluate release readiness")
    if result.readiness.status.value != "NOT_READY":
        raise RuntimeError(
            "Official static qualification without supplied requirements/runtime evidence must remain NOT_READY; "
            f"got {result.readiness.status.value}"
        )

    return {
        "controller": project.metadata.controller_name,
        "processor_type": project.metadata.processor_type,
        "software_revision": project.metadata.software_revision,
        "source_sha256": source_sha256,
        "inventory": actual_inventory,
        "instruction_semantic_coverage": project.instruction_semantic_coverage,
        "branch_semantic_coverage": project.branch_semantic_coverage,
        "branch_modeled": project.branch_rung_semantic_count,
        "branch_unmodeled": unmodeled_branches,
        "fat_candidates": len(engineering.fat_tests),
        "static_outcome": engineering.outcome.value,
        "support_contract": profile["static_contract"],
        "readiness": result.readiness.status.value,
    }


def _qualify_component_rejection(path: Path) -> dict[str, object]:
    try:
        run_production_verification_v5(path)
    except L5XError as exc:
        message = str(exc)
        if "full-project" not in message and "full project" not in message:
            raise RuntimeError(f"Component export failed for unexpected reason: {message}") from exc
        return {"component_export_rejected": True, "reason": message}
    raise RuntimeError("AOI component export was incorrectly accepted as a full-project production input")


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify DevAgent against pinned official Rockwell L5X artifacts")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-rockwell-official-") as directory_name:
        directory = Path(directory_name)
        full_path, full_sha256 = _download(_FULL_PROJECT, directory)
        component_path, component_sha256 = _download(_COMPONENT_EXPORT, directory)
        report = {
            "schema": "devagent-rockwell-official-qualification-v1",
            "upstream_repository": "RockwellAutomation/ra-logix-cicd",
            "upstream_commit": _UPSTREAM_COMMIT,
            "artifacts": {
                str(_FULL_PROJECT["name"]): {
                    "git_blob_sha1": _FULL_PROJECT["git_blob_sha1"],
                    "sha256": full_sha256,
                    "size": _FULL_PROJECT["size"],
                },
                str(_COMPONENT_EXPORT["name"]): {
                    "git_blob_sha1": _COMPONENT_EXPORT["git_blob_sha1"],
                    "sha256": component_sha256,
                    "size": _COMPONENT_EXPORT["size"],
                },
            },
            "full_project": _qualify_full_project(full_path, full_sha256),
            "component_contract": _qualify_component_rejection(component_path),
            "result": "PASS",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())