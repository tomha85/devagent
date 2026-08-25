from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from devagent.autonomy import AgentTask, AgentTaskResult, AutonomyError, ParallelAgentCoordinator
from devagent.providers import ScriptedFakeProvider


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def _clean_repo(root: Path) -> None:
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "DevAgent Qualification")
    _git(root, "config", "user.email", "qualification@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


def test_parallel_coordinator_bounds_concurrency_and_preserves_input_order(tmp_path: Path) -> None:
    _clean_repo(tmp_path)
    barrier = threading.Barrier(2)

    def runner(task: AgentTask) -> AgentTaskResult:
        barrier.wait(timeout=5)
        time.sleep(0.02)
        return AgentTaskResult(task.id, "VERIFIED", f"run-{task.id}", f"/tmp/{task.id}")

    coordinator = ParallelAgentCoordinator(tmp_path, max_parallel=2, runner=runner)
    tasks = tuple(AgentTask(f"task-{index}", "Inspect repository") for index in range(4))

    results = coordinator.run(tasks)

    assert [item.id for item in results] == [item.id for item in tasks]
    assert all(item.outcome == "VERIFIED" for item in results)
    assert coordinator.pool.peak_active == 2


def test_parallel_coordinator_rejects_dirty_source_before_spawning(tmp_path: Path) -> None:
    _clean_repo(tmp_path)
    (tmp_path / "developer.py").write_text("uncommitted = True\n", encoding="utf-8")
    coordinator = ParallelAgentCoordinator(
        tmp_path,
        runner=lambda task: AgentTaskResult(task.id, "VERIFIED", "run", "/tmp/worktree"),
    )

    with pytest.raises(AutonomyError, match="clean repository"):
        coordinator.run((AgentTask("one", "Inspect repository"),))


def _fixture(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (root / "calculator.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import divide\n\n"
        "def test_divide():\n"
        "    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "DevAgent Qualification")
    _git(root, "config", "user.email", "qualification@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


def _responses() -> list[dict[str, object]]:
    return [
        {
            "_role": "understand",
            "problem": "The division function does not handle a zero divisor.",
            "expected_behavior": "A zero divisor returns None and normal division remains unchanged.",
            "affected_paths": ["calculator.py", "tests/test_calculator.py"],
            "root_cause": "calculator.py directly evaluates a / b without a zero-divisor guard.",
            "evidence": [
                {"statement": "divide directly returns a / b.", "paths": ["calculator.py"], "confidence": 1.0},
                {"statement": "The existing test covers only a non-zero divisor.", "paths": ["tests/test_calculator.py"], "confidence": 1.0},
            ],
            "proposed_solution": ["Add a minimal zero-divisor guard.", "Add a focused regression test."],
            "confidence": 0.99,
        },
        {
            "_role": "plan",
            "files_to_inspect": ["calculator.py", "tests/test_calculator.py"],
            "implementation": ["Add the regression test.", "Add the zero-divisor guard."],
            "verification": [["python", "-m", "pytest", "-q"], ["git", "diff", "--check"]],
            "rationale": "The source and importing test are the complete affected working set.",
        },
        {
            "_role": "implement",
            "actions": [
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "tests/test_calculator.py",
                        "old": "def test_divide():\n    assert divide(10, 2) == 5\n",
                        "new": "def test_divide():\n    assert divide(10, 2) == 5\n\n"
                        "def test_divide_by_zero():\n    assert divide(10, 0) is None\n",
                    },
                },
                {
                    "tool": "replace_text",
                    "arguments": {
                        "path": "calculator.py",
                        "old": "def divide(a, b):\n    return a / b\n",
                        "new": "def divide(a, b):\n    if b == 0:\n        return None\n    return a / b\n",
                    },
                },
            ],
            "summary": ["Handled a zero divisor.", "Added regression coverage."],
        },
        {
            "_role": "review",
            "approved": True,
            "issues": [],
            "summary": "Focused and covered.",
        },
    ]


def test_two_real_devagents_run_in_distinct_isolated_worktrees(tmp_path: Path) -> None:
    _fixture(tmp_path)
    coordinator = ParallelAgentCoordinator(
        tmp_path,
        max_parallel=2,
        provider_factory=lambda: ScriptedFakeProvider(_responses()),
    )
    requirement = "Fix divide so a zero divisor returns None and add a regression test."

    results = coordinator.run((AgentTask("alpha", requirement), AgentTask("beta", requirement)))

    assert [item.outcome for item in results] == ["VERIFIED", "VERIFIED"]
    roots = [Path(item.working_root).resolve() for item in results]
    assert len(set(roots)) == 2
    assert all(root != tmp_path.resolve() for root in roots)
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == "def divide(a, b):\n    return a / b\n"
