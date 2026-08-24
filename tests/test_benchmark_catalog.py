from __future__ import annotations

import json
from pathlib import Path


def test_benchmark_catalog_references_existing_pytest_nodes() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog_path = root / "evaluation" / "benchmark_v1.json"
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["primary_invariant"] == "false_verified == 0"
    cases = payload["cases"]
    assert len(cases) >= 15

    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    for case in cases:
        node = case["pytest_node"]
        file_part, function_name = node.split("::", 1)
        target = root / file_part
        assert target.is_file(), node
        source = target.read_text(encoding="utf-8")
        assert f"def {function_name}(" in source, node
        assert case["category"]
        assert case["expected"]
