from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from devagent.controls.ir import build_controls_ir, controls_ir_payload
from devagent.controls.manifest import build_authoring_manifest
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.parser import load_control_system_spec
from devagent.controls.schema import ControlSpecError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent controls",
        description=(
            "Validate and inspect deterministic controls-authoring specifications. "
            "This command does not connect to PLCs or vendor engineering software."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate a controls specification")
    validate.add_argument("spec", type=Path)

    inspect = sub.add_parser("inspect", help="Print normalized authoring intent and Controls IR")
    inspect.add_argument("spec", type=Path)

    return parser


def _validate(path: Path) -> int:
    spec = load_control_system_spec(path)
    ir = build_controls_ir(spec)
    manifest = build_authoring_manifest(spec, ir)
    print("CONTROL_SPEC=PASS")
    print(f"PROJECT={manifest['project_id']}")
    print(f"CONTROLLERS={manifest['controller_count']}")
    print(f"EQUIPMENT={manifest['equipment_count']}")
    print(f"SPEC_SHA256={manifest['spec_sha256']}")
    print(f"CONTROLS_IR_SHA256={manifest['controls_ir_sha256']}")
    return 0


def _inspect(path: Path) -> int:
    spec = load_control_system_spec(path)
    ir = build_controls_ir(spec)
    payload = {
        "manifest": build_authoring_manifest(spec, ir),
        "normalized_spec": normalized_spec_payload(spec),
        "controls_ir": controls_ir_payload(ir),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "validate":
            return _validate(args.spec)
        if args.command == "inspect":
            return _inspect(args.spec)
        raise AssertionError(args.command)
    except (ControlSpecError, OSError) as exc:
        print(f"DevAgent controls failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
