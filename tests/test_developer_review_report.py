from __future__ import annotations

import subprocess
from pathlib import Path

from devagent.models import (
    AcceptanceCriterion,
    ChangeMetrics,
    Outcome,
    RepositoryModel,
    ReviewDecision,
    RiskLevel,
    RunResult,
    TaskSpec,
    TaskType,
    VerificationResult,
)
from devagent.report import render_report
from devagent.technical_review import analyze_developer_review


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init").returncode == 0
    assert _git(root, "config", "user.name", "DevAgent Test").returncode == 0
    assert _git(root, "config", "user.email", "devagent-test@example.com").returncode == 0
    (root / "calculator.py").write_text(
        "def divide(a, b):\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "from calculator import divide\n\n\n"
        "def test_divide():\n"
        "    assert divide(8, 2) == 4\n",
        encoding="utf-8",
    )
    assert _git(root, "add", "calculator.py", "test_calculator.py").returncode == 0
    assert _git(root, "commit", "-m", "baseline").returncode == 0
    return root


def test_analyze_developer_review_lists_changed_functions_and_unit_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "calculator.py").write_text(
        "def divide(a, b):\n"
        "    return a / b\n\n\n"
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "from calculator import divide, multiply\n\n\n"
        "def test_divide():\n"
        "    assert divide(8, 2) == 4\n\n\n"
        "def test_multiply_positive():\n"
        "    assert multiply(3, 4) == 12\n\n\n"
        "def test_multiply_negative():\n"
        "    assert multiply(-2, 5) == -10\n\n\n"
        "def test_multiply_zero():\n"
        "    assert multiply(7, 0) == 0\n",
        encoding="utf-8",
    )

    evidence = analyze_developer_review(root, ["calculator.py", "test_calculator.py"])

    symbols = {(item.name, item.change) for item in evidence.changed_symbols}
    tests = {(item.name, item.change) for item in evidence.test_cases}
    assert ("multiply", "ADDED") in symbols
    assert ("test_divide", "UNCHANGED") in tests
    assert ("test_multiply_positive", "ADDED") in tests
    assert ("test_multiply_negative", "ADDED") in tests
    assert ("test_multiply_zero", "ADDED") in tests
    assert evidence.test_files == ["test_calculator.py"]


def test_developer_report_explains_why_symbols_tests_acceptance_and_completeness(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "calculator.py").write_text(
        "def divide(a, b):\n"
        "    return a / b\n\n\n"
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "from calculator import divide, multiply\n\n\n"
        "def test_divide():\n"
        "    assert divide(8, 2) == 4\n\n\n"
        "def test_multiply_positive():\n"
        "    assert multiply(3, 4) == 12\n",
        encoding="utf-8",
    )
    developer_review = analyze_developer_review(root, ["calculator.py", "test_calculator.py"])

    result = RunResult(
        outcome=Outcome.VERIFIED,
        task=TaskSpec(
            task_type=TaskType.FEATURE,
            goal="Add multiplication support",
            requires_code_change=True,
            requires_tests=True,
            acceptance_criteria=[
                AcceptanceCriterion(
                    "multiply(a, b) returns the product",
                    evidence=["calculator.py", "python -m pytest -q"],
                ),
                AcceptanceCriterion(
                    "Existing divide behavior remains covered",
                    evidence=["test_calculator.py", "python -m pytest -q"],
                ),
            ],
            risk=RiskLevel.LOW,
        ),
        repository=RepositoryModel(
            root=str(root),
            kind="single-component",
            components=[],
            facts=[],
            git_branch="master",
            git_head=_git(root, "rev-parse", "HEAD").stdout.strip(),
        ),
        run_id="developer-review-test",
        run_dir=str(root / ".devagent" / "runs" / "developer-review-test"),
        root_cause="The calculator has division support but no multiplication API.",
        implementation=["Add multiply(a, b) without changing divide behavior", "Add regression and feature tests"],
        changes=ChangeMetrics(
            files_changed=2,
            lines_added=7,
            lines_deleted=1,
            paths=["calculator.py", "test_calculator.py"],
        ),
        verification=[
            VerificationResult(
                command=("python", "-m", "pytest", "-q"),
                exit_code=0,
                duration_seconds=0.12,
                stdout="2 passed",
                stderr="",
                classification=None,
                revision=2,
                phase="final",
                tests_run=2,
                tests_passed=2,
            )
        ],
        review=ReviewDecision(True, [], "The implementation is minimal and matches the requested behavior."),
        not_run=[],
        recommendations=[],
        state_history=[],
        working_root=str(root),
        developer_review=developer_review,
    )

    report = render_report(result)

    assert "DEVAGENT ENGINEERING REVIEW REPORT" in report
    assert "WHY THIS CHANGE" in report
    assert "FUNCTIONS / CLASSES / SYMBOLS CHANGED" in report
    assert "ADDED | function | calculator.py" in report
    assert "multiply" in report
    assert "TEST CASES / UNIT TESTS" in report
    assert "test_multiply_positive" in report
    assert "ACCEPTANCE CRITERIA + EVIDENCE" in report
    assert "AC-1 [REQUIRED]" in report
    assert "evidence: calculator.py" in report
    assert "VERIFICATION MATRIX" in report
    assert "phase=final" in report
    assert "COMPLETENESS ASSESSMENT" in report
    assert "Required acceptance criteria evidenced: 2/2" in report
    assert "COMPLETE FOR DEVELOPER REVIEW" in report
    assert "DEVELOPER REVIEW CHECKLIST" in report
