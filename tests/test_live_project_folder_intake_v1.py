from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from devagent.live.assist_cli import _build_parser, _resolve_project_input
from devagent.live.project_folder import LiveProjectFileKind, inspect_live_project_folder


def _write_l5x(path: Path, name: str = "WarehouseDemo") -> Path:
    path.write_text(
        f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="{name}" TargetType="Controller">
  <Controller Use="Target" Name="{name}" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><AddOnInstructionDefinitions /><Tags />
    <Programs /><Tasks />
  </Controller>
</RSLogix5000Content>''',
        encoding="utf-8",
    )
    return path


def test_folder_auto_selects_single_rockwell_export_and_inventories_supporting_files(tmp_path: Path) -> None:
    project = _write_l5x(tmp_path / "warehouse.L5X")
    (tmp_path / "IO_List.csv").write_text("address,tag\nI:0,Photoeye\n", encoding="utf-8")
    (tmp_path / "Tag_Descriptions.csv").write_text("tag,description\nRunCmd,Conveyor run command\n", encoding="utf-8")
    (tmp_path / "FAT_tests.md").write_text("# FAT\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("Conveyor shall stop on fault.\n", encoding="utf-8")

    intake = inspect_live_project_folder(tmp_path)

    assert intake.primary_project == project.resolve()
    assert intake.vendor == "ROCKWELL"
    assert len(intake.files) == 5
    kinds = {item.relative_path: item.kind for item in intake.files}
    assert kinds["warehouse.L5X"] is LiveProjectFileKind.PLC_ENGINEERING
    assert kinds["IO_List.csv"] is LiveProjectFileKind.IO_LIST
    assert kinds["Tag_Descriptions.csv"] is LiveProjectFileKind.TAG_DESCRIPTION
    assert kinds["FAT_tests.md"] is LiveProjectFileKind.FAT_TEST
    assert kinds["requirements.txt"] is LiveProjectFileKind.REQUIREMENTS
    rendered = intake.render_text()
    assert "Authoritative PLC engineering input" in rendered
    assert "IO_LIST: 1" in rendered
    assert "TAG_DESCRIPTION: 1" in rendered
    assert "must not override PLC logic semantics" in rendered


def test_folder_with_multiple_rockwell_exports_fails_closed_until_primary_is_explicit(tmp_path: Path) -> None:
    first = _write_l5x(tmp_path / "line1.L5X", "Line1")
    _write_l5x(tmp_path / "line2.L5X", "Line2")

    with pytest.raises(ValueError, match="multiple Rockwell .* candidates"):
        inspect_live_project_folder(tmp_path)

    intake = inspect_live_project_folder(tmp_path, primary_project=Path("line1.L5X"))
    assert intake.primary_project == first.resolve()
    assert intake.vendor == "ROCKWELL"


def test_primary_project_must_stay_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_l5x(workspace / "line.L5X")
    outside = _write_l5x(tmp_path / "outside.L5X", "Outside")

    with pytest.raises(ValueError, match="must resolve inside"):
        inspect_live_project_folder(workspace, primary_project=outside)


def test_folder_scan_ignores_common_dependency_directories(tmp_path: Path) -> None:
    _write_l5x(tmp_path / "line.L5X")
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "noise.txt").write_text("ignore", encoding="utf-8")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "noise.json").write_text("{}", encoding="utf-8")

    intake = inspect_live_project_folder(tmp_path)
    assert [item.relative_path for item in intake.files] == ["line.L5X"]


def test_assist_parser_accepts_project_folder_without_positional_project(tmp_path: Path) -> None:
    _write_l5x(tmp_path / "line.L5X")
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--project-folder",
            str(tmp_path),
            "--endpoint",
            "opc.tcp://127.0.0.1:4841/devagent/simulator/",
        ]
    )

    project, intake = _resolve_project_input(args)
    assert project == (tmp_path / "line.L5X").resolve()
    assert intake is not None
    assert intake.root == tmp_path.resolve()


def test_assist_input_contract_rejects_conflicting_or_missing_project_sources(tmp_path: Path) -> None:
    direct = _write_l5x(tmp_path / "line.L5X")
    both = argparse.Namespace(
        project=direct,
        project_folder=tmp_path,
        primary_project=None,
    )
    with pytest.raises(ValueError, match="either a direct project path or --project-folder"):
        _resolve_project_input(both)

    missing = argparse.Namespace(project=None, project_folder=None, primary_project=None)
    with pytest.raises(ValueError, match="Provide a PLC engineering project path or --project-folder"):
        _resolve_project_input(missing)

    bad_primary = argparse.Namespace(
        project=direct,
        project_folder=None,
        primary_project=Path("other.L5X"),
    )
    with pytest.raises(ValueError, match="only valid with --project-folder"):
        _resolve_project_input(bad_primary)
