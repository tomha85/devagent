from __future__ import annotations

from pathlib import Path

import pytest

from devagent.artifacts import RunArtifacts
from devagent.discovery import discover_repository
from devagent.retrieval import RetrievalBudget, retrieve_context, task_terms
from devagent.workspace import Workspace


TASK = (
    "Handle division by zero safely without changing normal division behavior. "
    "Add a regression test and verify the application."
)


def _minimal_repository(root: Path) -> None:
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


def test_small_repository_inventory_supplies_source_and_related_test(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    repository = discover_repository(tmp_path, probe_capabilities=False)
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path))

    context = retrieve_context(workspace, repository, TASK, requires_tests=True)

    assert context["ranked_paths"] == ["calculator.py", "test_calculator.py"]
    assert context["diagnostics"]["exact_lexical_matches"] == 0
    assert context["diagnostics"]["fallback"] == "small-repository inventory"
    assert context["snippets"]["calculator.py"] == "def divide(a, b):\n    return a / b\n"
    assert "assert divide(10, 2) == 5" in context["snippets"]["test_calculator.py"]
    assert {
        (relationship["source"], relationship["test"], relationship["kind"])
        for relationship in context["relationships"]
    } >= {("calculator.py", "test_calculator.py", "python_import")}


def test_identifier_and_concept_normalization_is_conservative() -> None:
    terms = task_terms(
        "division authentication configuration reconnection retry_count camelCase hyphenated-term"
    )
    assert {"divide", "auth", "config", "reconnect", "retry", "count", "camel", "case"} <= set(terms)


def test_large_repository_fallback_remains_bounded(tmp_path: Path) -> None:
    for index in range(40):
        (tmp_path / f"module_{index}.py").write_text(
            f"VALUE_{index} = {index}\n",
            encoding="utf-8",
        )
    (tmp_path / "tests").mkdir()
    for index in range(10):
        (tmp_path / "tests" / f"test_module_{index}.py").write_text(
            f"from module_{index} import VALUE_{index}\n\n"
            f"def test_value_{index}():\n    assert VALUE_{index} == {index}\n",
            encoding="utf-8",
        )
    repository = discover_repository(tmp_path, probe_capabilities=False)
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path))
    budget = RetrievalBudget(
        max_files=8,
        max_chars=8_000,
        max_per_file_chars=1_000,
        max_fallback_files=3,
        small_repository_max_files=5,
    )

    context = retrieve_context(
        workspace,
        repository,
        "Improve an undocumented behavior and add regression coverage",
        requires_tests=True,
        budget=budget,
        max_chars=8_000,
    )

    assert context["diagnostics"]["fallback"] == "bounded structural coverage"
    assert len(context["ranked_paths"]) <= 8
    assert len(context["diagnostics"]["fallback_paths"]) <= 3
    assert sum(len(value) for value in context["snippets"].values()) <= 8_000


def test_inventory_does_not_follow_text_symlinks_outside_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET_VALUE = 'do not read'\n", encoding="utf-8")
    try:
        (root / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    (root / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    repository = discover_repository(root, probe_capabilities=False)
    workspace = Workspace(root, RunArtifacts(root))

    context = retrieve_context(workspace, repository, "Inspect values")

    assert context["ranked_paths"] == ["safe.py"]
    assert "linked.py" not in context["snippets"]
    assert all("SECRET_VALUE" not in snippet for snippet in context["snippets"].values())
