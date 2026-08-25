from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QualificationCase:
    id: str
    category: str
    pytest_node: str
    expected: str


@dataclass(frozen=True)
class QualificationCaseResult:
    id: str
    category: str
    pytest_node: str
    expected: str
    passed: bool
    returncode: int
    duration_seconds: float


@dataclass(frozen=True)
class QualificationSummary:
    cases_total: int
    cases_passed: int
    cases_failed: int
    pass_rate: float
    fully_qualified: bool


def load_catalog(path: Path) -> tuple[dict[str, Any], tuple[QualificationCase, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("qualification catalog schema_version must be 2")
    if payload.get("primary_invariant") != "false_verified == 0":
        raise ValueError("qualification catalog must preserve false_verified == 0")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("qualification catalog must contain cases")

    cases: list[QualificationCase] = []
    seen_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("qualification case must be an object")
        case = QualificationCase(
            id=str(raw.get("id", "")).strip(),
            category=str(raw.get("category", "")).strip(),
            pytest_node=str(raw.get("pytest_node", "")).strip(),
            expected=str(raw.get("expected", "")).strip(),
        )
        if not all((case.id, case.category, case.pytest_node, case.expected)):
            raise ValueError("qualification case fields must be non-empty")
        if case.id in seen_ids:
            raise ValueError(f"duplicate qualification case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)

    required_categories = set(payload.get("required_categories", []))
    present_categories = {case.category for case in cases}
    missing = sorted(required_categories - present_categories)
    if missing:
        raise ValueError(f"qualification catalog missing required categories: {', '.join(missing)}")
    return payload, tuple(cases)


def run_qualification(
    root: Path,
    catalog_path: Path,
    *,
    report_path: Path | None = None,
) -> tuple[QualificationSummary, tuple[QualificationCaseResult, ...]]:
    root = root.resolve()
    _, cases = load_catalog(catalog_path.resolve())
    results: list[QualificationCaseResult] = []

    for case in cases:
        started = time.monotonic()
        environment = os.environ.copy()
        environment["DEVAGENT_PRODUCTION_QUALIFICATION"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", case.pytest_node],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.monotonic() - started
        results.append(
            QualificationCaseResult(
                id=case.id,
                category=case.category,
                pytest_node=case.pytest_node,
                expected=case.expected,
                passed=completed.returncode == 0,
                returncode=completed.returncode,
                duration_seconds=duration,
            )
        )
        status = "PASS" if completed.returncode == 0 else "FAIL"
        print(f"[{status}] {case.id} ({case.category})")
        if completed.returncode != 0:
            if completed.stdout.strip():
                print(completed.stdout.rstrip())
            if completed.stderr.strip():
                print(completed.stderr.rstrip(), file=sys.stderr)

    passed = sum(result.passed for result in results)
    total = len(results)
    summary = QualificationSummary(
        cases_total=total,
        cases_passed=passed,
        cases_failed=total - passed,
        pass_rate=passed / total if total else 0.0,
        fully_qualified=bool(total) and passed == total,
    )

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": asdict(summary),
                    "cases": [asdict(item) for item in results],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    print(
        "Qualification: "
        f"{summary.cases_passed}/{summary.cases_total} passed "
        f"({summary.pass_rate:.1%})"
    )
    return summary, tuple(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DevAgent functional qualification cases")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("evaluation/benchmark_v4.json"),
        help="Qualification catalog path",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".devagent/functional-qualification.json"),
        help="Machine-readable qualification report path",
    )
    args = parser.parse_args(argv)
    summary, _ = run_qualification(Path.cwd(), args.catalog, report_path=args.report)
    return 0 if summary.fully_qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
