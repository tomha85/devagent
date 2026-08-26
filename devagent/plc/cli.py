from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from devagent.plc.analysis import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, plc_jsonable
from devagent.plc.report import render_fat_report
from devagent.plc.rockwell_l5x import L5XError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent plc",
        description="Analyze a Rockwell full-project L5X export using deterministic PLC engineering verification",
    )
    parser.add_argument("project", type=Path, help="Rockwell Studio 5000 full-project .L5X export")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write canonical IR, graph, FAT tests, verification evidence, and report here",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the FAT report without writing run artifacts",
    )
    return parser


def _default_output_dir(project: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return project.expanduser().resolve(strict=False).parent / ".devagent" / "plc-runs" / run_id


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(plc_jsonable(value), indent=2) + "\n", encoding="utf-8")


def _persist_run(output_dir: Path, result, report: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "canonical_ir.json", result.project)
    _write_json(output_dir / "dependency_graph.json", result.graph)
    _write_json(output_dir / "fat_tests.json", result.fat_tests)
    _write_json(
        output_dir / "static_verification.json",
        {
            "outcome": result.outcome,
            "checks": result.static_checks,
            "limitations": result.limitations,
        },
    )
    (output_dir / "fat_report.md").write_text(report, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        print("DevAgent PLC is working...")
        print("[1/6] ROCKWELL L5X VALIDATION")
        result = analyze_rockwell_l5x(args.project)
        print("[2/6] CANONICAL PLC IR")
        print("[3/6] DEPENDENCY GRAPH")
        print("[4/6] FAT TEST MODEL")
        print("[5/6] STATIC VERIFICATION")
        report = render_fat_report(result)
        print("[6/6] FAT REPORT")
        print(report, end="")

        if not args.no_write:
            output_dir = args.output_dir.expanduser().resolve(strict=False) if args.output_dir else _default_output_dir(args.project)
            _persist_run(output_dir, result, report)
            print(f"Artifacts: {output_dir}")

        return 0 if result.outcome is PLCOutcome.STATICALLY_VERIFIED else 2
    except (L5XError, OSError, ValueError) as exc:
        print(f"DevAgent PLC failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
