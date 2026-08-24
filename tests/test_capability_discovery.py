from __future__ import annotations

from pathlib import Path

from devagent.discovery import discover_repository
from devagent.models import CapabilityProvenance


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


def test_pytest_is_promoted_only_after_successful_local_collection(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)

    repository = discover_repository(tmp_path)

    capabilities = [
        capability
        for capability in repository.capabilities
        if capability.kind == "test"
    ]
    assert len(capabilities) == 1
    capability = capabilities[0]
    assert capability.command == ("python", "-m", "pytest", "-q")
    assert capability.provenance is CapabilityProvenance.PROBED
    assert capability.trusted
    assert capability.tests_collected == 1
    assert "successful pytest collection" in capability.provenance_detail
    assert any("collection probe: PASS" in item for item in repository.capability_diagnostics)
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / "__pycache__").exists()


def test_manifest_capability_remains_explicit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )
    (tmp_path / "test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )

    repository = discover_repository(tmp_path)

    capability = next(item for item in repository.capabilities if item.kind == "test")
    assert capability.provenance is CapabilityProvenance.EXPLICIT
    assert capability.trusted
    assert repository.capability_diagnostics == []
