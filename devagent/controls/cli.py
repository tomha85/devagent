from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from devagent.controls.catalog import STANDARDS
from devagent.controls.build import (
    ControlsBuildError,
    build_controls_project,
    verify_controls_build,
)
from devagent.controls.ir import build_controls_ir, controls_ir_payload
from devagent.controls.manifest import build_authoring_manifest
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.parser import load_control_system_spec
from devagent.controls.portal import serve_portal
from devagent.controls.review import create_review_request
from devagent.controls.rules import evaluate_controls_rules, rules_pass
from devagent.controls.schema import ControlSpecError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent controls",
        description=(
            "Deterministic controls authoring, Rockwell staging generation, "
            "Ignition staging generation, round-trip verification, FAT planning, "
            "and self-service engineering workflows. Production controller/HMI "
            "deployment is intentionally outside this command."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("catalog", help="Print the qualified V1 equipment standards catalog")

    validate = sub.add_parser("validate", help="Validate a controls specification and company standards")
    validate.add_argument("spec", type=Path)

    inspect = sub.add_parser("inspect", help="Print normalized authoring intent and Controls IR")
    inspect.add_argument("spec", type=Path)

    build = sub.add_parser(
        "build",
        aliases=["generate"],
        help="Generate and self-verify deterministic Rockwell/Ignition/FAT staging artifacts",
    )
    build.add_argument("spec", type=Path)
    build.add_argument("--output-dir", "--output", dest="output_dir", required=True, type=Path)

    verify = sub.add_parser("verify", help="Verify a previously generated controls build")
    verify.add_argument("build_dir", type=Path)

    review = sub.add_parser(
        "request-review",
        help="Create a hash-bound engineering review request for a verified build",
    )
    review.add_argument("build_dir", type=Path)
    review.add_argument("--requested-by", required=True)
    review.add_argument("--output", required=True, type=Path)

    portal = sub.add_parser("portal", help="Run the local self-service Controls engineering portal")
    portal.add_argument("--workspace", type=Path, default=Path(".devagent/controls-portal"))
    portal.add_argument("--host", default="127.0.0.1")
    portal.add_argument("--port", type=int, default=8765)
    portal.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback bind. Use only behind an operator-managed secure boundary.",
    )

    return parser


def _rule_payload(spec) -> list[dict[str, str]]:
    return [
        {
            "id": item.id,
            "status": item.status,
            "subject": item.subject,
            "summary": item.summary,
        }
        for item in evaluate_controls_rules(spec)
    ]


def _catalog() -> int:
    payload = {
        "schema": "devagent-controls-catalog-v1",
        "standards": [
            {
                "id": item.id,
                "equipment_type": item.equipment_type,
                "required_commands": list(item.required_commands),
                "required_status": list(item.required_status),
                "generated_outputs": list(item.generated_outputs),
                "default_faceplate": item.default_faceplate,
            }
            for item in sorted(STANDARDS.values(), key=lambda value: value.id)
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _validate(path: Path) -> int:
    spec = load_control_system_spec(path)
    ir = build_controls_ir(spec)
    manifest = build_authoring_manifest(spec, ir)
    rules = evaluate_controls_rules(spec)
    standards_ok = rules_pass(rules)
    print("CONTROL_SPEC=PASS")
    print(f"COMPANY_STANDARDS={'PASS' if standards_ok else 'FAIL'}")
    print(f"PROJECT={manifest['project_id']}")
    print(f"CONTROLLERS={manifest['controller_count']}")
    print(f"EQUIPMENT={manifest['equipment_count']}")
    print(f"SPEC_SHA256={manifest['spec_sha256']}")
    print(f"CONTROLS_IR_SHA256={manifest['controls_ir_sha256']}")
    for item in rules:
        if item.status != "PASS":
            print(f"{item.status} {item.id} {item.subject}: {item.summary}")
    return 0 if standards_ok else 2


def _inspect(path: Path) -> int:
    spec = load_control_system_spec(path)
    ir = build_controls_ir(spec)
    payload = {
        "manifest": build_authoring_manifest(spec, ir),
        "normalized_spec": normalized_spec_payload(spec),
        "controls_ir": controls_ir_payload(ir),
        "company_rules": _rule_payload(spec),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def _build(path: Path, output_dir: Path) -> int:
    result = build_controls_project(path, output_dir)
    print("CONTROLS_BUILD=PASS")
    print(f"OUTPUT_DIR={result.output_dir}")
    print(f"READINESS={result.readiness['status']}")
    print(f"SPEC_SHA256={result.manifest['spec_sha256']}")
    print(f"CONTROLS_IR_SHA256={result.manifest['controls_ir_sha256']}")
    print("FAT_EXECUTION=NOT_RUN")
    print("PRODUCTION_DEPLOYMENT=NOT_PERFORMED")
    return 0


def _verify(path: Path) -> int:
    result = verify_controls_build(path)
    print(f"CONTROLS_BUILD_VERIFY={result['status']}")
    for error in result.get("errors", []):
        print(f"ERROR={error}")
    if result["status"] == "PASS":
        print(f"PROJECT={result['project_id']}")
        print(f"SPEC_SHA256={result['spec_sha256']}")
        print(f"CONTROLS_IR_SHA256={result['controls_ir_sha256']}")
    return 0 if result["status"] == "PASS" else 2


def _request_review(path: Path, *, requested_by: str, output: Path) -> int:
    request = create_review_request(
        path,
        requested_by=requested_by,
        output_path=output,
    )
    print("CONTROLS_REVIEW_REQUEST=PASS")
    print(f"PROJECT={request['project_id']}")
    print(f"REQUESTED_BY={request['requested_by']}")
    print(f"OUTPUT={output.expanduser().resolve(strict=False)}")
    print("PRODUCTION_RELEASE_READY=false")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "catalog":
            return _catalog()
        if args.command == "validate":
            return _validate(args.spec)
        if args.command == "inspect":
            return _inspect(args.spec)
        if args.command in {"build", "generate"}:
            return _build(args.spec, args.output_dir)
        if args.command == "verify":
            return _verify(args.build_dir)
        if args.command == "request-review":
            return _request_review(
                args.build_dir,
                requested_by=args.requested_by,
                output=args.output,
            )
        if args.command == "portal":
            serve_portal(
                args.workspace,
                host=args.host,
                port=args.port,
                allow_remote=args.allow_remote,
            )
            return 0
        raise AssertionError(args.command)
    except (ControlSpecError, ControlsBuildError, OSError, ValueError) as exc:
        print(f"DevAgent controls failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
