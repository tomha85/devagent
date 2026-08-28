from __future__ import annotations

import argparse
from pathlib import Path

from devagent.plc.schneider_real_st_gap_analysis import (
    analyze_schneider_real_st_gaps,
    write_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect and cluster PARTIAL Schneider Control Expert Structured Text statements "
            "from the current V9 analyzer without widening deterministic theorem coverage."
        )
    )
    parser.add_argument("source", type=Path, help="Control Expert .XEF or supported granular export path")
    parser.add_argument("--top", type=int, default=10, help="Number of highest-frequency clusters to print")
    parser.add_argument(
        "--samples-per-cluster",
        type=int,
        default=5,
        help="Number of representative statements retained for each cluster",
    )
    parser.add_argument(
        "--include-source-text",
        action="store_true",
        help=(
            "Include local PLC source snippets in the generated reports. Off by default so "
            "reports can be shared without automatically copying proprietary source text."
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path(".devagent/schneider-real-st-gap-analysis.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path(".devagent/schneider-real-st-gap-analysis.md"),
    )
    args = parser.parse_args()

    result = analyze_schneider_real_st_gaps(
        args.source,
        samples_per_cluster=max(0, args.samples_per_cluster),
        include_source_text=args.include_source_text,
    )
    write_reports(result, json_path=args.json, markdown_path=args.markdown)

    print("========== SCHNEIDER REAL ST GAP ANALYSIS ==========")
    print(f"Source SHA256: {result.source_sha256}")
    print(f"Outcome: {result.outcome}")
    print(f"V9 support contract: {result.support_contract}")
    print(f"ST statements: {result.total_st_statements}")
    print(f"PARTIAL ST statements: {result.partial_st_statements}")
    print()
    print("Top clusters:")
    for index, cluster in enumerate(result.clusters[: max(0, args.top)], start=1):
        features = ", ".join(f"{name}={count}" for name, count in cluster.features[:5]) or "none"
        print(f"{index:2d}. {cluster.category:40s} {cluster.count:4d}  {features}")
        for sample in cluster.samples:
            print(f"      {sample.owner} / {sample.locator} / {sample.statement_id}")
            if args.include_source_text and sample.source_text:
                print(f"         {' '.join(sample.source_text.split())}")
    print()
    print(f"JSON report: {args.json}")
    print(f"Markdown report: {args.markdown}")
    print("The analyzer is diagnostic only; no PARTIAL statement is promoted to FULL by this command.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
