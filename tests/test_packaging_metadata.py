from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from devagent import __version__


def test_pypi_distribution_and_cli_metadata_are_release_consistent() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "devagent-ai"
    assert project["version"] == __version__
    assert project["scripts"]["devagent"] == "devagent.cli:main"


def test_source_checkout_remains_editable_install_compatible() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "dev" in project["optional-dependencies"]
    assert "pytest>=8.0" in project["optional-dependencies"]["dev"]
