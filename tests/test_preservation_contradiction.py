from __future__ import annotations

from pathlib import Path

from conftest import commit_all
from devagent.models import Evidence, Outcome, Understanding
from devagent.orchestrator import DevAgent
from devagent.providers import ScriptedFakeProvider


def _contradictory_understanding() -> dict[str, object]:
    return {
        "_role": "understand",
        "problem": (
            "calculator.py defines only divide(a, b); addition is missing and the requested "
            "existing multiply behavior is not present in the baseline."
        ),
        "expected_behavior": (
            "Add addition support while preserving existing divide and multiply behavior."
        ),
        "affected_paths": ["calculator.py", "test_calculator.py"],
        "root_cause": (
            "Addition has not been implemented; despite the requirement mentioning existing "
            "multiply behavior, no multiply function or test appears in the repository."
        ),
        "evidence": [
            {
                "statement": "calculator.py contains divide but no multiply function.",
                "paths": ["calculator.py"],
                "confidence": 1.0,
            },
            {
                "statement": "test_calculator.py contains only divide coverage and no multiply test.",
                "paths": ["test_calculator.py"],
                "confidence": 1.0,
            },
        ],
        "proposed_solution": ["Add add(a, b) and focused addition tests."],
        "confidence": 0.99,
    }


def test_preservation_conflict_is_detected_before_implementation(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import divide\n\ndef test_divide():\n    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )
    understanding = Understanding(
        problem="Addition is missing; existing multiply behavior is also absent.",
        expected_behavior="Add addition while preserving existing divide and multiply behavior.",
        affected_paths=["calculator.py", "test_calculator.py"],
        root_cause="There is no multiply function in the current baseline.",
        evidence=[
            Evidence("calculator.py has divide and no multiply function.", ("calculator.py",), 1.0)
        ],
        proposed_solution=["Add addition support."],
        confidence=0.99,
    )

    assert understanding.preservation_conflicts() == ["multiply"]
    assert understanding.implementation_ready(tmp_path) is False


def test_noncontradictory_preservation_remains_implementation_ready(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    understanding = Understanding(
        problem="Division by zero is not handled.",
        expected_behavior="Handle zero safely while normal division remains unchanged.",
        affected_paths=["calculator.py"],
        root_cause="divide evaluates a / b without a zero guard.",
        evidence=[Evidence("divide directly evaluates a / b.", ("calculator.py",), 1.0)],
        proposed_solution=["Add a zero-divisor guard."],
        confidence=0.99,
    )

    assert understanding.preservation_conflicts() == []
    assert understanding.implementation_ready(tmp_path) is True


def test_compound_preservation_requirement_cannot_false_verify(git_repo: Path) -> None:
    (git_repo / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    (git_repo / "test_calculator.py").write_text(
        "from calculator import divide\n\ndef test_divide():\n    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )
    commit_all(git_repo)

    first = _contradictory_understanding()
    second = _contradictory_understanding()
    provider = ScriptedFakeProvider([first, second])
    result = DevAgent(provider).run(
        git_repo,
        (
            "Add addition support with an add(a, b) function. Preserve existing divide and "
            "multiply behavior. Add tests for positive addition, negative values, and zero."
        ),
    )

    assert result.outcome is Outcome.BLOCKED
    assert result.changes.files_changed == 0
    assert [call["role"] for call in provider.calls] == ["understand", "understand"]
    assert any("Evidence gate rejected implementation" in item for item in result.not_run)
    assert (git_repo / "calculator.py").read_text(encoding="utf-8") == (
        "def divide(a, b):\n    return a / b\n"
    )
