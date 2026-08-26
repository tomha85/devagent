#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from devagent.plc.warehouse_benchmark import (
    generate_warehouse_benchmark,
    score_warehouse_benchmark,
    sha256,
    write_score,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and score the deterministic Rockwell warehouse sortation golden benchmark"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for generated L5X ground truth, requirements, manifest, and score",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate the benchmark artifacts without running DevAgent scoring",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    files = generate_warehouse_benchmark(args.output_dir)
    summary = {
        "baseline": str(files["baseline"]),
        "baseline_sha256": sha256(files["baseline"]),
        "defective": str(files["defective"]),
        "defective_sha256": sha256(files["defective"]),
        "requirements": str(files["requirements"]),
        "manifest": str(files["manifest"]),
    }
    if args.generate_only:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    score = score_warehouse_benchmark(
        files["defective"],
        baseline_project=files["baseline"],
        requirements_path=files["requirements"],
    )
    report = args.output_dir / "benchmark_score.json"
    write_score(report, score)
    print(json.dumps({**summary, "score": score, "score_path": str(report)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
