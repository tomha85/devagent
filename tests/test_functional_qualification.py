from __future__ import annotations

import json
from pathlib import Path

import pytest

from devagent.models import TaskType
from devagent.qualification import load_catalog, run_qualification
from devagent.tasking import compile_task


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("Fix the broken reconnect behavior", TaskType.BUG_FIX),
        ("Add CSV export support", TaskType.FEATURE),
        ("Traceback when the service starts", TaskType.RUNTIME_ERROR),
        ("A failing test in the parser suite", TaskType.TEST_FAILURE),
        ("The project does not build", TaskType.BUILD_FAILURE),
        ("Write tests for the parser contract", TaskType.UNIT_TEST),
        ("Refactor parser internals without behavior changes", TaskType.REFACTOR),
        ("Migrate users from integer IDs to UUIDs", TaskType.MIGRATION),
        ("Optimize request latency", TaskType.PERFORMANCE),
        ("Document the repository ownership model", TaskType.GENERAL_ENGINEERING_TASK),
    ],
)
def test_all_public_task_types_have_deterministic_classification_examples(
    requirement: str,
    expected: TaskType,
) -> None:
    assert compile_task(requirement).task_type is expected


def test_v2_catalog_covers_required_functional_categories() -> None:
    root = Path(__file__).resolve().parents[1]
    payload, cases = load_catalog(root / "evaluation" / "benchmark_v2.json")

    assert payload["primary_invariant"] == "false_verified == 0"
    assert len(cases) >= 35
    present = {case.category for case in cases}
    assert set(payload["required_categories"]) <= present
    assert len({case.id for case in cases}) == len(cases)

    for case in cases:
        file_part, function_name = case.pytest_node.split("::", 1)
        target = root / file_part
        assert target.is_file(), case.pytest_node
        source = target.read_text(encoding="utf-8")
        assert f"def {function_name}(" in source, case.pytest_node


def test_catalog_loader_rejects_missing_required_category(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "primary_invariant": "false_verified == 0",
                "required_categories": ["truthfulness", "source_control_safety"],
                "cases": [
                    {
                        "id": "truth",
                        "category": "truthfulness",
                        "pytest_node": "tests/test_example.py::test_truth",
                        "expected": "PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required categories"):
        load_catalog(catalog)


def test_qualification_runner_returns_non_full_when_any_case_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    tests = root / "test_sample.py"
    tests.write_text(
        "def test_good():\n    assert True\n\n"
        "def test_bad():\n    assert False\n",
        encoding="utf-8",
    )
    catalog = root / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "primary_invariant": "false_verified == 0",
                "required_categories": ["qualification"],
                "cases": [
                    {
                        "id": "good",
                        "category": "qualification",
                        "pytest_node": "test_sample.py::test_good",
                        "expected": "PASS",
                    },
                    {
                        "id": "bad",
                        "category": "qualification",
                        "pytest_node": "test_sample.py::test_bad",
                        "expected": "PASS",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    report = root / "report.json"

    summary, results = run_qualification(root, catalog, report_path=report)

    assert summary.cases_total == 2
    assert summary.cases_passed == 1
    assert summary.cases_failed == 1
    assert summary.pass_rate == pytest.approx(0.5)
    assert summary.fully_qualified is False
    assert [item.passed for item in results] == [True, False]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["fully_qualified"] is False
