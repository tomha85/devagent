from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from devagent.evaluation import EvaluationCaseResult, EvaluationMetrics
from devagent.models import Outcome
from devagent.realworld import (
    BenchmarkMutation,
    OracleResult,
    RealWorldCase,
    _apply_mutations,
    load_catalog,
    run_oracle,
    score_realworld_case,
)


def _case() -> RealWorldCase:
    return RealWorldCase(
        id="example-bug",
        category="bug_fix",
        repository_url="https://github.com/example/project",
        revision="a" * 40,
        requirement="Fix the injected behavior and keep existing behavior working.",
        mutations=(BenchmarkMutation("app.py", "return 1", "return 2"),),
        oracle_command=("python", "-m", "pytest", "-q"),
        max_files_changed=3,
        max_lines_changed=80,
    )


def _metrics(**overrides: object) -> EvaluationMetrics:
    values: dict[str, object] = {
        "task_success": True,
        "acceptance_criteria_supported": 2,
        "acceptance_criteria_total": 2,
        "acceptance_coverage": 1.0,
        "new_regressions": 0,
        "files_changed": 2,
        "lines_changed": 12,
        "iterations": 1,
        "model_calls": 4,
        "tool_calls": 8,
        "runtime_seconds": 0.5,
        "outcome": Outcome.VERIFIED,
        "final_verification_passed": True,
        "review_approved": True,
        "source_head_unchanged": True,
        "source_status_unchanged": True,
    }
    values.update(overrides)
    return EvaluationMetrics(**values)  # type: ignore[arg-type]


def _evaluation(**metric_overrides: object) -> EvaluationCaseResult:
    return EvaluationCaseResult(
        name="example-bug",
        category="bug_fix",
        expected_outcomes=(Outcome.VERIFIED,),
        metrics=_metrics(**metric_overrides),
        passed=True,
        false_verified=False,
        unexpected_blocked=False,
        violations=(),
    )


def _oracle(passed: bool) -> OracleResult:
    return OracleResult(
        command=("python", "-m", "pytest", "-q"),
        passed=passed,
        exit_code=0 if passed else 1,
        duration_seconds=0.1,
        stdout="",
        stderr="",
    )


def test_catalog_requires_pinned_public_github_repository(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "primary_invariant": "false_verified == 0",
        "cases": [
            {
                "id": "case-1",
                "category": "bug_fix",
                "repository_url": "https://github.com/example/project",
                "revision": "b" * 40,
                "requirement": "Fix the deterministic injected bug.",
                "mutations": [
                    {"path": "app.py", "old": "return 1", "new": "return 2"}
                ],
                "oracle_command": ["python", "-m", "pytest", "-q"],
            }
        ],
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(payload), encoding="utf-8")

    cases = load_catalog(catalog)

    assert cases[0].revision == "b" * 40
    assert cases[0].repository_url == "https://github.com/example/project"

    payload["cases"][0]["repository_url"] = "https://evil.example/repo"
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="github.com"):
        load_catalog(catalog)

    payload["cases"][0]["repository_url"] = "https://github.com/example/project"
    payload["cases"][0]["revision"] = "main"
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="40-character"):
        load_catalog(catalog)


def test_mutation_is_exact_and_confined(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")

    _apply_mutations(
        tmp_path,
        (BenchmarkMutation("app.py", "return 1", "return 2"),),
    )

    assert "return 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="drift"):
        _apply_mutations(
            tmp_path,
            (BenchmarkMutation("app.py", "return 1", "return 3"),),
        )


def test_external_oracle_failure_marks_verified_result_false_verified() -> None:
    result = score_realworld_case(
        _case(),
        _evaluation(),
        _oracle(False),
        _oracle(False),
        "c" * 40,
    )

    assert not result.passed
    assert result.false_verified
    assert "external_oracle_failed" in result.violations


def test_realworld_case_requires_failing_mutated_baseline_and_passing_final_oracle() -> None:
    result = score_realworld_case(
        _case(),
        _evaluation(),
        _oracle(False),
        _oracle(True),
        "d" * 40,
    )

    assert result.passed
    assert not result.false_verified
    assert result.violations == ()


def test_oracle_environment_does_not_expose_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "show_env.py"
    script.write_text(
        "import os\n"
        "print('HOME=' + os.environ.get('HOME', ''))\n"
        "print('OPENAI_API_KEY=' + os.environ.get('OPENAI_API_KEY', ''))\n"
        "print('CARGO_REGISTRY_TOKEN=' + os.environ.get('CARGO_REGISTRY_TOKEN', ''))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "secret-cargo")
    original_home = os.environ.get("HOME", "")

    result = run_oracle(tmp_path, (sys.executable, "show_env.py"))

    assert result.passed
    assert "secret-openai" not in result.stdout
    assert "secret-cargo" not in result.stdout
    assert f"HOME={original_home}" not in result.stdout
    assert "benchmark-oracle-home" in result.stdout
