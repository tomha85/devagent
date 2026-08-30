from __future__ import annotations

from pathlib import Path

from devagent.live.project_folder import LiveProjectFileKind, inspect_live_project_folder


def test_repository_live_example_is_a_complete_project_folder_package() -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "live"
    intake = inspect_live_project_folder(root)

    assert intake.vendor == "ROCKWELL"
    assert intake.primary_project.name == "warehouse_commissioning_demo.L5X"
    by_name = {item.relative_path: item.kind for item in intake.files}
    assert by_name["warehouse_commissioning_demo.L5X"] is LiveProjectFileKind.PLC_ENGINEERING
    assert by_name["IO_List.csv"] is LiveProjectFileKind.IO_LIST
    assert by_name["Tag_Descriptions.csv"] is LiveProjectFileKind.TAG_DESCRIPTION
    assert by_name["requirements.md"] is LiveProjectFileKind.REQUIREMENTS
    assert by_name["FAT_tests.csv"] is LiveProjectFileKind.FAT_TEST
    assert len(intake.supplemental_files) == 4
