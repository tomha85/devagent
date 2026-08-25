from __future__ import annotations

from pathlib import Path

from conftest import commit_all
from devagent.models import EngineeringPlan, Outcome
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider


def test_engineering_plan_drops_path_scoped_git_diff_but_keeps_real_checks() -> None:
    plan = EngineeringPlan(
        files_to_inspect=["calculator.py", "test_calculator.py"],
        implementation=["add subtraction", "add regression coverage"],
        verification=[
            ("python", "-m", "pytest", "-q"),
            ("git", "diff", "--", "calculator.py", "test_calculator.py"),
            ("git", "diff", "--check"),
        ],
        rationale="Use repository tests and deterministic final diff review.",
    )

    assert plan.verification == [
        ("python", "-m", "pytest", "-q"),
        ("git", "diff", "--check"),
    ]


def test_path_scoped_git_diff_in_plan_does_not_block_feature_implementation(
    git_repo: Path,
) -> None:
    (git_repo / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\n'
        'name = "devagent-e2e-plan-normalization"\n'
        'version = "0.1.0"\n\n'
        '[tool.pytest.ini_options]\n'
        'testpaths = ["."]\n',
        encoding="utf-8",
    )
    (git_repo / "calculator.py").write_text(
        "def divide(a, b):\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (git_repo / "test_calculator.py").write_text(
        "from calculator import divide\n\n\n"
        "def test_divide():\n"
        "    assert divide(8, 2) == 4\n",
        encoding="utf-8",
    )
    commit_all(git_repo)

    responses: list[dict[str, object]] = [
        {
            "_role": "understand",
            "problem": "calculator.py has divide(a, b) but no subtract(a, b), and tests cover only divide.",
            "expected_behavior": "subtract returns a - b for positive and negative values while divide behavior is preserved.",
            "affected_paths": ["calculator.py", "test_calculator.py"],
            "root_cause": "The subtraction API and its regression coverage have not been implemented.",
            "evidence": [
                {
                    "statement": "calculator.py defines divide(a, b) only.",
                    "paths": ["calculator.py"],
                    "confidence": 1.0,
                },
                {
                    "statement": "test_calculator.py imports and tests divide only.",
                    "paths": ["test_calculator.py"],
                    "confidence": 1.0,
                },
            ],
            "proposed_solution": [
                "Add subtract(a, b) without changing divide.",
                "Add positive and negative subtraction regression tests.",
            ],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["calculator.py", "test_calculator.py"],
            "implementation": [
                "Add subtract(a, b) to calculator.py.",
                "Add positive and negative regression tests while retaining test_divide.",
            ],
            "verification": [
                ["python", "-m", "pytest", "-q"],
                ["git", "diff", "--", "calculator.py", "test_calculator.py"],
            ],
            "rationale": "Use the repository test suite; DevAgent performs final diff inspection independently.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "calculator.py",
                        "old": "def divide(a, b):\n    return a / b\n",
                        "new": (
                            "def divide(a, b):\n"
                            "    return a / b\n\n\n"
                            "def subtract(a, b):\n"
                            "    return a - b\n"
                        ),
                    },
                },
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "test_calculator.py",
                        "old": (
                            "from calculator import divide\n\n\n"
                            "def test_divide():\n"
                            "    assert divide(8, 2) == 4\n"
                        ),
                        "new": (
                            "from calculator import divide, subtract\n\n\n"
                            "def test_divide():\n"
                            "    assert divide(8, 2) == 4\n\n\n"
                            "def test_subtract_positive():\n"
                            "    assert subtract(8, 3) == 5\n\n\n"
                            "def test_subtract_negative():\n"
                            "    assert subtract(-2, 3) == -5\n"
                        ),
                    },
                },
            ],
            "summary": [
                "Added subtract(a, b).",
                "Added positive and negative regression coverage while preserving divide.",
            ],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "The patch is minimal, preserves divide, and adds the requested tested subtraction behavior.",
        },
    ]

    provider = ScriptedFakeProvider(responses)
    result = DevAgent(provider).run(
        git_repo,
        (
            "Add a subtract(a, b) function to calculator.py. Add regression tests "
            "for positive and negative values. Preserve divide behavior."
        ),
    )

    assert result.outcome is Outcome.VERIFIED
    assert result.review is not None and result.review.approved
    assert result.changes.paths == ["calculator.py", "test_calculator.py"]
    assert any(call["role"] == "implement" for call in provider.calls)
    assert any(call["role"] == "review" for call in provider.calls)
    assert not any(
        "git diff -- calculator.py test_calculator.py" in item
        for item in result.not_run
    )
    final_pytest = next(
        item
        for item in result.verification
        if item.phase == "final"
        and item.command == ("python", "-m", "pytest", "-q")
    )
    assert final_pytest.passed
    assert final_pytest.tests_run == 3
    assert final_pytest.tests_passed == 3
    working_root = Path(result.working_root)
    assert "def subtract(a, b):" in (working_root / "calculator.py").read_text(encoding="utf-8")
    assert (git_repo / "calculator.py").read_text(encoding="utf-8") == (
        "def divide(a, b):\n"
        "    return a / b\n"
    )
