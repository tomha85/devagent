from __future__ import annotations

import json
from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.warehouse_benchmark import (
    DEFECTS,
    generate_warehouse_benchmark,
    score_warehouse_benchmark,
    sha256,
)


def test_warehouse_benchmark_generation_is_deterministic_and_large(tmp_path: Path) -> None:
    first = generate_warehouse_benchmark(tmp_path / "first")
    second = generate_warehouse_benchmark(tmp_path / "second")

    assert sha256(first["baseline"]) == sha256(second["baseline"])
    assert sha256(first["defective"]) == sha256(second["defective"])
    assert first["baseline"].read_text(encoding="utf-8") != first["defective"].read_text(encoding="utf-8")

    baseline = analyze_rockwell_l5x(first["baseline"])
    defective = analyze_rockwell_l5x(first["defective"])

    for engineering in (baseline, defective):
        project = engineering.project
        assert len(project.programs) == 7
        assert len(project.tags) > 450
        assert len(project.routines) > 70
        assert len(project.rungs) > 250
        assert project.st_statement_total >= 8
        assert len(project.aois) == 1
        assert project.aoi_call_total == 4
        assert project.aoi_call_bound_count == 4
        assert project.metadata.full_project is True


def test_warehouse_benchmark_ground_truth_contains_exact_seeded_defects(tmp_path: Path) -> None:
    files = generate_warehouse_benchmark(tmp_path / "benchmark")
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    seeded = json.loads(files["seeded_defects"].read_text(encoding="utf-8"))["defects"]

    assert len(DEFECTS) == 20
    assert [item["id"] for item in DEFECTS] == [f"D{index:02d}" for index in range(1, 21)]
    assert [item["id"] for item in seeded] == [item["id"] for item in DEFECTS]
    assert len(manifest["seeded_defects"]) == 20
    assert manifest["equipment"] == {
        "conveyors": 40,
        "diverters": 8,
        "chutes": 16,
        "photoeyes": 80,
        "vfds": 40,
        "barcode_tunnels": 1,
        "encoder_tracking_systems": 1,
        "aoi_instances": 4,
    }
    assert manifest["acceptance_targets"]["false_verified_defects"] == 0
    assert manifest["acceptance_targets"]["critical_defect_recall"] == 1.0


def test_warehouse_benchmark_scores_current_engine_without_rewarding_ignorance(tmp_path: Path) -> None:
    files = generate_warehouse_benchmark(tmp_path / "benchmark")
    score = score_warehouse_benchmark(
        files["defective"],
        baseline_project=files["baseline"],
        requirements_path=files["requirements"],
    )

    assert score["metrics"]["seeded_defects"] == 20
    assert score["metrics"]["readiness"] == "NOT_READY"
    assert score["metrics"]["requirements_total"] == 16
    assert 0.0 <= score["metrics"]["overall_recall"] <= 1.0
    assert 0.0 <= score["metrics"]["critical_recall"] <= 1.0
    assert 0.0 <= score["metrics"]["high_recall"] <= 1.0
    assert score["coverage"]["aoi_call"] == 1.0

    # A requirement that DevAgent cannot map is a benchmark miss, not a
    # detection. Every defect marked detected must have a real requirement,
    # risk, or regression signal.
    for defect in score["defects"]:
        if defect["detected"]:
            assert defect["signals"]
    for defect_id in score["metrics"]["not_mapped_defects"]:
        item = next(defect for defect in score["defects"] if defect["id"] == defect_id)
        if item["detected"]:
            assert set(item["signals"]) & {"RISK", "REGRESSION"}

    # This is the non-negotiable trust gate even before recall targets are met.
    assert score["metrics"]["false_verified_defects"] == []
