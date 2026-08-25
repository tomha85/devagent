from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devagent.discovery import discover_repository


def _production_only() -> None:
    if os.getenv("DEVAGENT_PRODUCTION_QUALIFICATION") != "1":
        pytest.skip("huge-monorepo stress runs only through the production qualification gate")


def test_deep_dotnet_manifest_is_recovered_beyond_12000_file_walk_frontier(tmp_path: Path) -> None:
    _production_only()
    filler = tmp_path / "aaa"
    filler.mkdir()
    for index in range(12_025):
        (filler / f"f{index:05d}.txt").write_text("", encoding="utf-8")

    deep = tmp_path / "zzz" / "service"
    deep.mkdir(parents=True)
    (deep / "Service.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
        '</PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    (deep / "Service.cs").write_text(
        "namespace Deep; public static class Service { public static int Value => 1; }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    repository = discover_repository(tmp_path, probe_capabilities=False)

    assert repository.inventory_file_count == 12_000
    component = next(item for item in repository.components if item.path == "zzz/service")
    assert "zzz/service/Service.csproj" in component.manifests
    assert "c#" in component.languages
    assert any(item.command == ("dotnet", "build", "zzz/service/Service.csproj") for item in component.capabilities)
