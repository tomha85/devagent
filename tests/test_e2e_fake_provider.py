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


def _minimal_fixture(root: Path) -> str:
    (root / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "from calculator import divide\n\n"
        "def test_divide():\n"
        "    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )
    return commit_all(root)


def _minimal_responses() -> list[dict[str, object]]:
    return [
        {
            "_role": "understand",
            "problem": "The division function does not handle a zero divisor.",
            "expected_behavior": "A zero divisor returns None and normal division remains unchanged.",
            "affected_paths": ["calculator.py", "test_calculator.py"],
            "root_cause": "calculator.py directly evaluates a / b without a zero-divisor guard.",
            "evidence": [
                {
                    "statement": "divide directly returns a / b.",
                    "paths": ["calculator.py"],
                    "confidence": 1.0,
                },
                {
                    "statement": "The existing test covers only a non-zero divisor.",
                    "paths": ["test_calculator.py"],
                    "confidence": 1.0,
                },
            ],
            "proposed_solution": [
                "Add a minimal zero-divisor guard.",
                "Add a focused regression test.",
            ],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["calculator.py", "test_calculator.py"],
            "implementation": ["Add the regression test.", "Add the zero-divisor guard."],
            "verification": [
                ["python", "-m", "pytest", "-q"],
                ["git", "diff", "--check"],
            ],
            "rationale": "The source and its importing test are the complete affected working set.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "test_calculator.py",
                        "old": (
                            "def test_divide():\n"
                            "    assert divide(10, 2) == 5\n"
                        ),
                        "new": (
                            "def test_divide():\n"
                            "    assert divide(10, 2) == 5\n\n"
                            "def test_divide_by_zero():\n"
                            "    assert divide(10, 0) is None\n"
                        ),
                    },
                },
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "calculator.py",
                        "old": "def divide(a, b):\n    return a / b\n",
                        "new": (
                            "def divide(a, b):\n"
                            "    if b == 0:\n"
                            "        return None\n"
                            "    return a / b\n"
                        ),
                    },
                },
            ],
            "summary": ["Handled a zero divisor.", "Added regression coverage."],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "The patch is minimal, evidence-backed, and fully verified.",
        },
    ]


def test_minimal_unknown_repository_is_discovered_probed_and_verified(
    git_repo: Path,
) -> None:
    original_head = _minimal_fixture(git_repo)
    messages: list[str] = []

    provider = ScriptedFakeProvider(_minimal_responses())
    result = DevAgent(
        provider,
        verbose=True,
        status=messages.append,
    ).run(
        git_repo,
        (
            "Handle division by zero safely without changing normal division behavior. "
            "Add a regression test and verify the application."
        ),
    )

    assert result.outcome is Outcome.VERIFIED
    assert result.changes.paths == ["calculator.py", "test_calculator.py"]
    expected_states = [
        AgentState.DISCOVER,
        AgentState.UNDERSTAND,
        AgentState.BASELINE,
        AgentState.PLAN,
        AgentState.IMPLEMENT,
        AgentState.VERIFY_TARGETED,
        AgentState.VERIFY_BROAD,
        AgentState.REVIEW,
        AgentState.FINAL_VERIFY,
        AgentState.SUCCESS,
    ]
    positions = [result.state_history.index(state) for state in expected_states]
    assert positions == sorted(positions)
    baseline = next(
        item
        for item in result.verification
        if item.phase == "baseline" and item.command == ("python", "-m", "pytest", "-q")
    )
    final = next(
        item
        for item in result.verification
        if item.phase == "final" and item.command == ("python", "-m", "pytest", "-q")
    )
    assert baseline.passed and baseline.tests_run == 1 and baseline.tests_passed == 1
    assert final.passed and final.tests_run == 2 and final.tests_passed == 2
    targeted_diff_check = next(
        item
        for item in result.verification
        if item.phase == "targeted"
        and item.command == ("git", "diff", "--check")
    )
    final_diff_check = next(
        item
        for item in result.verification
        if item.phase == "final"
        and item.command == ("git", "diff", "--check")
    )
    assert targeted_diff_check.passed
    assert final_diff_check.passed
    assert final_diff_check.revision == max(
        item.revision for item in result.verification
    )
    assert all(
        capability.command != ("git", "diff", "--check")
        for capability in result.repository.capabilities
    )
    plan_payload = next(
        call["payload"] for call in provider.calls if call["role"] == "plan"
    )
    assert plan_payload["builtin_verification"] == [["git", "diff", "--check"]]
    working_root = Path(result.working_root)
    assert working_root != git_repo
    assert not working_root.is_relative_to(git_repo)
    assert "if b == 0" in (working_root / "calculator.py").read_text(encoding="utf-8")
    assert (git_repo / "calculator.py").read_text(encoding="utf-8") == (
        "def divide(a, b):\n    return a / b\n"
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == original_head
    )
    assert any("[RETRIEVAL] fallback: small-repository inventory" in item for item in messages)
    assert any(
        "[VERIFICATION_DISCOVERY]" in item and "capability promoted" in item
        for item in messages
    )
    assert any("[WORKTREE] isolated: true" in item for item in messages)


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
