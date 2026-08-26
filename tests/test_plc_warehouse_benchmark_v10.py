from __future__ import annotations

import ast
import json
from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from evaluation.benchmarks.warehouse_sortation_v10 import (
    DEFECTS,
    generate_warehouse_benchmark,
    sha256,
)
from evaluation.benchmarks.warehouse_v10_scorer import score_warehouse_v10


def test_warehouse_benchmark_generation_is_deterministic_and_large(tmp_path: Path) -> None:
    first = generate_warehouse_benchmark(tmp_path / "first")
    second = generate_warehouse_benchmark(tmp_path / "second")

    assert sha256(first["baseline"]) == sha256(second["baseline"])
    assert sha256(first["defective"]) == sha256(second["defective"])
    assert first["baseline"].read_text(encoding="utf-8") != first["defective"].read_text(encoding="utf-8")

    for path in (first["baseline"], first["defective"]):
        project = analyze_rockwell_l5x(path).project
        assert len(project.programs) == 7
        assert len(project.tags) > 450
        assert len(project.routines) > 70
        assert len(project.rungs) > 250
        assert project.st_statement_total >= 8
        assert len(project.aois) == 1
        assert project.aoi_call_total == 4
        assert project.aoi_call_bound_count == 4
        assert project.metadata.full_project is True


def test_hidden_ground_truth_contains_exact_twenty_seeded_defects(tmp_path: Path) -> None:
    files = generate_warehouse_benchmark(tmp_path / "benchmark")
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    seeded = json.loads(files["seeded_defects"].read_text(encoding="utf-8"))["defects"]

    assert len(DEFECTS) == 20
    assert [item["id"] for item in DEFECTS] == [f"D{index:02d}" for index in range(1, 21)]
    assert [item["id"] for item in seeded] == [item["id"] for item in DEFECTS]
    assert manifest["acceptance_targets"]["false_verified_defects"] == 0
    assert manifest["acceptance_targets"]["critical_defect_recall"] == 1.0


def test_production_core_cannot_import_evaluation_ground_truth() -> None:
    root = Path(__file__).resolve().parents[1] / "devagent"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("evaluation"):
                violations.append(str(path.relative_to(root.parent)))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("evaluation"):
                        violations.append(str(path.relative_to(root.parent)))
    assert violations == []


def test_warehouse_score_uses_real_project_plan_without_rewarding_ignorance(tmp_path: Path) -> None:
    files = generate_warehouse_benchmark(tmp_path / "benchmark")
    score = score_warehouse_v10(
        files["defective"],
        baseline_project=files["baseline"],
        requirements_path=files["requirements"],
    )

    assert score["metrics"]["seeded_defects"] == 20
    assert score["metrics"]["readiness"] == "NOT_READY"
    assert score["metrics"]["requirements_total"] == 16
    assert score["metrics"]["test_intents"] > 0
    assert score["project_test_plan"]["behavior_count"] > 0
    assert score["project_test_plan"]["test_intent_count"] >= score["metrics"]["fat_tests"]
    assert 0.0 <= score["metrics"]["overall_recall"] <= 1.0
    assert 0.0 <= score["metrics"]["critical_recall"] <= 1.0
    assert 0.0 <= score["metrics"]["high_recall"] <= 1.0

    # Trust is a hard gate even while recall/coverage targets are still product goals.
    assert score["metrics"]["false_verified_defects"] == []

    # Current generic planner should discover dynamic classes from semantics,
    # without any warehouse-specific rule in production code.
    scenarios = set(score["project_test_plan"]["scenarios"])
    assert "TIMER_NOT_EARLY" in scenarios
    assert "TIMER_AT_PRESET" in scenarios
    assert "RESET_PATH" in scenarios
    assert "COUNTER_STEP" in scenarios
    assert "AOI_INTERFACE" in scenarios
    assert "MULTI_WRITER_ORDERING" in scenarios

    # NOT_MAPPED is a miss; it never becomes a detection by itself.
    for defect_id in score["metrics"]["not_mapped_defects"]:
        item = next(defect for defect in score["defects"] if defect["id"] == defect_id)
        if item["detected"]:
            assert set(item["signals"]) & {"RISK", "REGRESSION"}
