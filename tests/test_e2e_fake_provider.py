from __future__ import annotations

import json
import subprocess
from pathlib import Path

from devagent.models import AgentState, Outcome
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider
from devagent.evaluation import evaluate

from conftest import commit_all


def _fixture(root: Path) -> str:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "math-fixture"\nversion = "0.0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (root / "math_utils.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_math_utils.py").write_text(
        "from math_utils import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n",
        encoding="utf-8",
    )
    return commit_all(root)


def _responses(review_approved: bool = True) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = [
        {
            "_role": "understand",
            "problem": "divide raises ZeroDivisionError when the denominator is zero",
            "expected_behavior": "division by zero returns None while normal division is preserved",
            "affected_paths": ["math_utils.py"],
            "root_cause": "divide delegates to Python division without guarding a zero denominator",
            "evidence": [
                {"statement": "The function directly evaluates a / b", "paths": ["math_utils.py"], "confidence": 1.0},
                {"statement": "Existing tests cover only non-zero division", "paths": ["tests/test_math_utils.py"], "confidence": 1.0},
            ],
            "proposed_solution": ["add a zero-denominator guard", "add a regression assertion"],
            "confidence": 0.98,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["math_utils.py", "tests/test_math_utils.py"],
            "implementation": ["add regression coverage", "add minimal guard"],
            "verification": [["python", "-m", "pytest", "-q"]],
            "rationale": "Two focused edits preserve the existing interface.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "tests/test_math_utils.py",
                        "old": "def test_divide():\n    assert divide(6, 3) == 2\n",
                        "new": "def test_divide():\n    assert divide(6, 3) == 2\n\n\ndef test_divide_by_zero():\n    assert divide(6, 0) is None\n",
                    },
                },
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "math_utils.py",
                        "old": "def divide(a, b):\n    return a / b\n",
                        "new": "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b\n",
                    },
                },
            ],
            "summary": ["Handled a zero denominator with the specified sentinel", "Added regression coverage"],
        },
        {
            "_role": "review",
            "approved": review_approved,
            "issues": [] if review_approved else [{"severity": "medium", "reason": "Use an explicit type annotation", "path": "math_utils.py"}],
            "summary": "Focused and covered" if review_approved else "One maintainability correction required",
        },
    ]
    if not review_approved:
        responses.extend(
            [
                {
                    "_role": "implement_review_fixes",
                    "actions": [
                        {
                            "tool": "replace_text",
                            "arguments": {
                                "path": "math_utils.py",
                                "old": "def divide(a, b):",
                                "new": "def divide(a: float, b: float) -> float | None:",
                            },
                        }
                    ],
                    "summary": "Added the requested explicit interface types",
                },
                {"_role": "review", "approved": True, "issues": [], "summary": "All findings resolved"},
            ]
        )
    return responses


def test_end_to_end_fixture_is_verified_without_source_control_publication(git_repo: Path) -> None:
    original_head = _fixture(git_repo)
    provider = ScriptedFakeProvider(_responses())
    result, metrics = evaluate(git_repo, "Handle division by zero and add a regression test", provider)

    assert result.outcome is Outcome.VERIFIED
    assert metrics.task_success and metrics.new_regressions == 0
    assert metrics.acceptance_criteria_supported == metrics.acceptance_criteria_total
    assert metrics.model_calls == 4 and metrics.tool_calls >= 4
    assert result.review and result.review.approved
    assert result.changes.paths == ["math_utils.py", "tests/test_math_utils.py"]
    assert any(item.phase == "baseline" and item.passed for item in result.verification)
    assert any(item.phase == "final" and item.passed for item in result.verification)
    working_root = Path(result.working_root)
    assert "if b == 0" in (working_root / "math_utils.py").read_text(encoding="utf-8")
    assert (git_repo / "math_utils.py").read_text(encoding="utf-8") == "def divide(a, b):\n    return a / b\n"
    backups = Path(result.run_dir) / "backups"
    assert (backups / "math_utils.py").read_text(encoding="utf-8") == "def divide(a, b):\n    return a / b\n"
    assert (backups / "tests" / "test_math_utils.py").is_file()
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, check=True, capture_output=True, text=True).stdout.strip()
    assert current_head == original_head
    assert subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=git_repo, check=True, capture_output=True, text=True).stdout.strip() == "fixture baseline"
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "VERIFIED"


def test_review_rejection_forces_edit_and_reverification(git_repo: Path) -> None:
    _fixture(git_repo)
    provider = ScriptedFakeProvider(_responses(review_approved=False))
    result = DevAgent(provider).run(git_repo, "Handle division by zero and add a regression test")
    assert result.outcome is Outcome.VERIFIED
    assert AgentState.IMPLEMENT in result.state_history[result.state_history.index(AgentState.REVIEW) + 1 :]
    assert "float | None" in (Path(result.working_root) / "math_utils.py").read_text(encoding="utf-8")
    final_revision = max(item.revision for item in result.verification)
    assert all(item.passed for item in result.verification if item.phase == "final" and item.revision == final_revision)


def test_insufficient_evidence_blocks_before_modification(git_repo: Path) -> None:
    _fixture(git_repo)
    weak = {
        "_role": "understand",
        "problem": "maybe bad",
        "expected_behavior": "unknown",
        "affected_paths": ["math_utils.py"],
        "root_cause": "",
        "evidence": [],
        "proposed_solution": [],
        "confidence": 0.2,
    }
    result = DevAgent(ScriptedFakeProvider([weak, weak])).run(git_repo, "Handle division by zero")
    assert result.outcome is Outcome.BLOCKED
    assert result.changes.files_changed == 0
    assert (git_repo / "math_utils.py").read_text(encoding="utf-8") == "def divide(a, b):\n    return a / b\n"


def test_failed_hypothesis_can_replan_before_a_focused_fix(git_repo: Path) -> None:
    _fixture(git_repo)
    base = _responses()
    initial_implementation = base[2]
    assert isinstance(initial_implementation["actions"], list)
    initial_implementation["actions"] = initial_implementation["actions"][:1]
    responses = [
        base[0],
        base[1],
        initial_implementation,
        {
            "_role": "diagnose",
            "decision": "replan",
            "updated_hypothesis": "The regression test proves the missing guard is the remaining cause",
            "actions": [],
        },
        {**base[1], "_role": "replan"},
        {
            "_role": "implement_replan",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "math_utils.py",
                        "old": "def divide(a, b):\n    return a / b\n",
                        "new": "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b\n",
                    },
                }
            ],
            "summary": "Implemented the evidence-backed guard",
        },
        base[3],
    ]
    result = DevAgent(ScriptedFakeProvider(responses)).run(git_repo, "Handle division by zero and add a regression test")
    assert result.outcome is Outcome.VERIFIED
    diagnosis_index = result.state_history.index(AgentState.DIAGNOSE)
    assert AgentState.PLAN in result.state_history[diagnosis_index + 1 :]
    assert any(not item.passed for item in result.verification if item.phase == "targeted")
    assert all(item.passed for item in result.verification if item.phase == "final")
