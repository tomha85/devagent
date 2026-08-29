from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.live.errors import LiveConfigurationError
from devagent.live.plan import (
    LivePlanReferenceStatus,
    analyze_and_build_live_commission_plan,
    build_live_commission_plan,
    write_live_commission_plan,
)


def _tag(
    tag_id: str,
    name: str,
    *,
    scope: str = "controller",
    alias_for: str | None = None,
    external_access: str | None = None,
):
    return SimpleNamespace(
        id=tag_id,
        name=name,
        scope=scope,
        alias_for=alias_for,
        external_access=external_access,
    )


def _fat(
    test_id: str,
    output: str,
    *,
    preconditions=None,
    watch_tags=(),
):
    return SimpleNamespace(
        id=test_id,
        output_tag=output,
        preconditions={} if preconditions is None else preconditions,
        watch_tags=tuple(watch_tags),
    )


def _engineering(tags, tests, *, vendor="ROCKWELL", sha="project-sha"):
    project = SimpleNamespace(
        tags=list(tags),
        metadata=SimpleNamespace(vendor=vendor, source_sha256=sha),
    )
    return SimpleNamespace(project=project, fat_tests=list(tests))


def _build(engineering, **kwargs):
    return build_live_commission_plan(
        engineering,
        engineering_project_path=Path("/tmp/project.L5X"),
        plc_id=kwargs.pop("plc_id", "line1"),
        plc_name=kwargs.pop("plc_name", "Line 1"),
        endpoint=kwargs.pop("endpoint", "opc.tcp://127.0.0.1:4840/"),
        **kwargs,
    )


def test_plan_selects_exact_fat_output_precondition_and_watch_tags() -> None:
    engineering = _engineering(
        [
            _tag("T:RUN", "RunCmd"),
            _tag("T:READY", "DriveReady"),
            _tag("T:FAULT", "FaultCode"),
            _tag("T:OTHER", "Unrelated"),
        ],
        [
            _fat(
                "FAT-1",
                "RunCmd",
                preconditions={"DriveReady": True},
                watch_tags=("FaultCode", "RunCmd"),
            )
        ],
    )
    plan = _build(engineering)
    assert plan.complete is True
    assert plan.required_tag_ids == ("T:RUN", "T:READY", "T:FAULT")
    assert [item.reference for item in plan.references] == [
        "RunCmd",
        "DriveReady",
        "FaultCode",
    ]
    run = plan.references[0]
    assert run.roles == ("OUTPUT", "WATCH")
    assert run.fat_test_ids == ("FAT-1",)
    assert run.status is LivePlanReferenceStatus.RESOLVED
    assert run.selected_tag_id == "T:RUN"


def test_program_scope_disambiguates_duplicate_tag_names() -> None:
    engineering = _engineering(
        [
            _tag("MAIN:RUN", "RunCmd", scope="program:Main"),
            _tag("AUX:RUN", "RunCmd", scope="program:Aux"),
        ],
        [_fat("FAT-1", "Main.RunCmd")],
    )
    plan = _build(engineering)
    assert plan.complete is True
    assert plan.required_tag_ids == ("MAIN:RUN",)


def test_unqualified_duplicate_name_is_ambiguous_and_cannot_write(tmp_path) -> None:
    engineering = _engineering(
        [
            _tag("MAIN:RUN", "RunCmd", scope="program:Main"),
            _tag("AUX:RUN", "RunCmd", scope="program:Aux"),
        ],
        [_fat("FAT-1", "RunCmd")],
    )
    plan = _build(engineering)
    assert plan.complete is False
    item = plan.references[0]
    assert item.status is LivePlanReferenceStatus.AMBIGUOUS
    assert set(item.candidate_tag_ids) == {"MAIN:RUN", "AUX:RUN"}
    with pytest.raises(LiveConfigurationError, match="incomplete"):
        write_live_commission_plan(tmp_path / "commission.json", plan)
    assert not (tmp_path / "commission.json").exists()


def test_unmatched_reference_fails_closed() -> None:
    plan = _build(_engineering([_tag("T:RUN", "RunCmd")], [_fat("FAT-1", "Missing")]))
    assert plan.complete is False
    assert plan.references[0].status is LivePlanReferenceStatus.UNMATCHED
    assert plan.required_tag_ids == ()


def test_alias_reference_requires_explicit_mapping() -> None:
    plan = _build(
        _engineering(
            [_tag("T:ALIAS", "RunAlias", alias_for="RunCmd")],
            [_fat("FAT-1", "RunAlias")],
        )
    )
    assert plan.references[0].status is LivePlanReferenceStatus.ALIAS_REQUIRES_EXPLICIT
    assert plan.required_tag_ids == ()


def test_external_access_blocked_reference_is_not_selected() -> None:
    plan = _build(
        _engineering(
            [_tag("T:RUN", "RunCmd", external_access="None")],
            [_fat("FAT-1", "RunCmd")],
        )
    )
    assert plan.references[0].status is LivePlanReferenceStatus.EXTERNAL_ACCESS_BLOCKED
    assert plan.required_tag_ids == ()


def test_no_fat_runtime_references_cannot_generate_plan() -> None:
    engineering = _engineering([_tag("T:RUN", "RunCmd")], [])
    with pytest.raises(LiveConfigurationError, match="no FAT"):
        _build(engineering)


def test_invalid_plc_id_and_endpoint_fail_closed() -> None:
    engineering = _engineering([_tag("T:RUN", "RunCmd")], [_fat("FAT-1", "RunCmd")])
    with pytest.raises(LiveConfigurationError, match="must match"):
        _build(engineering, plc_id="../line")
    with pytest.raises(LiveConfigurationError, match="opc.tcp"):
        _build(engineering, endpoint="http://127.0.0.1")


def test_plan_limit_is_200_distinct_resolved_tags() -> None:
    tags = [_tag(f"T:{i}", f"Tag{i}") for i in range(201)]
    tests = [_fat(f"FAT-{i}", f"Tag{i}") for i in range(201)]
    with pytest.raises(LiveConfigurationError, match="V1 limit is 200"):
        _build(_engineering(tags, tests))


def test_writer_emits_consumable_config_and_provenance_report(tmp_path) -> None:
    plan = _build(
        _engineering(
            [_tag("T:RUN", "RunCmd"), _tag("T:READY", "DriveReady")],
            [_fat("FAT-1", "RunCmd", preconditions={"DriveReady": True})],
            vendor="SIEMENS",
            sha="sha-123",
        )
    )
    config_path, report_path = write_live_commission_plan(
        tmp_path / "commission.json",
        plan,
    )
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert config["schema"] == "devagent-live-commission-v1"
    plc = config["plcs"][0]
    assert plc["plc_id"] == "line1"
    assert plc["plc_name"] == "Line 1"
    assert plc["required_tag_ids"] == ["T:RUN", "T:READY"]
    assert plc["require_all_mappings"] is True
    assert "security" not in plc
    assert report["schema"] == "devagent-live-commission-plan-v1"
    assert report["mode"] == "READ_ONLY"
    assert report["complete"] is True
    assert report["vendor"] == "SIEMENS"
    assert report["project_sha256"] == "sha-123"
    assert report["config_sha256"] == hashlib.sha256(config_bytes).hexdigest()


def test_writer_refuses_existing_config_or_report(tmp_path) -> None:
    plan = _build(_engineering([_tag("T:RUN", "RunCmd")], [_fat("FAT-1", "RunCmd")]))
    target = tmp_path / "commission.json"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_live_commission_plan(target, plan)
    assert target.read_text(encoding="utf-8") == "existing"

    target.unlink()
    report = tmp_path / "commission.json.plan.json"
    report.write_text("existing-report", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_live_commission_plan(target, plan)
    assert not target.exists()
    assert report.read_text(encoding="utf-8") == "existing-report"


def test_analyze_wrapper_resolves_project_path_and_uses_loader(tmp_path) -> None:
    project_path = tmp_path / "project.XEF"
    project_path.write_text("fixture", encoding="utf-8")
    engineering = _engineering(
        [_tag("T:RUN", "RunCmd")],
        [_fat("FAT-1", "RunCmd")],
        vendor="SCHNEIDER",
    )
    calls = []

    def loader(path: Path):
        calls.append(path)
        return engineering

    plan = analyze_and_build_live_commission_plan(
        project_path,
        plc_id="line1",
        plc_name="Line 1",
        endpoint="opc.tcp://127.0.0.1:4840/",
        project_loader=loader,
    )
    assert calls == [project_path.resolve()]
    assert plan.engineering_project_path == project_path.resolve()
    assert plan.vendor == "SCHNEIDER"
