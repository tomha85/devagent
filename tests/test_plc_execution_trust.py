from __future__ import annotations

import json
from pathlib import Path

import pytest

from devagent.plc.execution_trust import load_execution_backend_registry, require_qualified_backend


def _write_registry(tmp_path: Path, backend: dict) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-execution-backend-registry-v1",
                "approved_by": "Controls Platform Owner",
                "approved_at": "2026-08-26T12:00:00Z",
                "backends": [backend],
            }
        ),
        encoding="utf-8",
    )
    return path


def _backend(**overrides) -> dict:
    item = {
        "id": "sim-qualified",
        "kind": "SIMULATOR",
        "status": "QUALIFIED",
        "project_sha256": ["a" * 64],
        "qualification_evidence": ["QUAL-SIM-001"],
    }
    item.update(overrides)
    return item


def test_registry_rejects_missing_project_scope_instead_of_implicit_global(tmp_path: Path) -> None:
    backend = _backend()
    backend.pop("project_sha256")
    path = _write_registry(tmp_path, backend)

    with pytest.raises(ValueError, match="requires explicit project_sha256 scope"):
        load_execution_backend_registry(path)


def test_registry_accepts_only_explicit_global_wildcard(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _backend(project_sha256="*"))
    registry = load_execution_backend_registry(path)

    assert registry is not None
    qualified = require_qualified_backend(registry, "sim-qualified", "b" * 64)
    assert qualified.project_sha256 == ("*",)


def test_registry_project_scope_remains_fail_closed_for_other_projects(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _backend(project_sha256=["a" * 64]))
    registry = load_execution_backend_registry(path)

    assert registry is not None
    with pytest.raises(ValueError, match="not qualified for this project"):
        require_qualified_backend(registry, "sim-qualified", "b" * 64)
