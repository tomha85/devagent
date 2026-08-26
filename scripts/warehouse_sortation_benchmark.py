#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.benchmarks.warehouse_sortation_v10 import generate_warehouse_benchmark, sha256
from evaluation.benchmarks.warehouse_v10_scorer import score_warehouse_v10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and score the isolated Rockwell warehouse sortation golden benchmark"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generate-only", action="store_true")
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

    score = score_warehouse_v10(
        files["defective"],
        baseline_project=files["baseline"],
        requirements_path=files["requirements"],
    )
    report = args.output_dir / "benchmark_score.json"
    report.write_text(json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**summary, "score": score, "score_path": str(report)}, indent=2, sort_keys=True))
    return 0 if not score["metrics"]["false_verified_defects"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
