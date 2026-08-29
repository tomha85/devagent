from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.live import commission
from devagent.live.errors import LiveConfigurationError
from devagent.live.workflow import LiveCommissioningState

NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


def _tag(tag_id: str, name: str = "RunCmd"):
    return SimpleNamespace(id=tag_id, name=name)


def _project(*tags):
    return SimpleNamespace(tags=list(tags), metadata=SimpleNamespace(vendor="TEST"))


def _write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "commission.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_plc(**overrides):
    value = {
        "plc_id": "line1",
        "plc_name": "Line 1",
        "endpoint": "opc.tcp://127.0.0.1:4840/line1/",
        "engineering_project": "project.L5X",
        "required_tag_ids": ["TAG:RUN"],
    }
    value.update(overrides)
    return value


def _base_config(**plc_overrides):
    return {
        "schema": "devagent-live-commission-v1",
        "plcs": [_base_plc(**plc_overrides)],
    }


def test_load_valid_config_resolves_project_and_builds_spec(tmp_path) -> None:
    project_path = tmp_path / "project.L5X"
    project_path.write_text("fixture", encoding="utf-8")
    seen = []

    def loader(path: Path):
        seen.append(path)
        return SimpleNamespace(project=_project(_tag("TAG:RUN")))

    path = _write_config(tmp_path, _base_config())
    loaded = commission.load_commissioning_config(path, project_loader=loader)
    assert seen == [project_path.resolve()]
    assert loaded.source_path == path.resolve()
    assert len(loaded.specs) == 1
    spec = loaded.specs[0]
    assert spec.connection.plc_id == "line1"
    assert spec.connection.display_name == "Line 1"
    assert spec.required_tag_ids == ("TAG:RUN",)
    assert spec.connection.security.password is None
    assert len(loaded.source_sha256) == 64


def test_config_rejects_direct_secret_values(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    path = _write_config(
        tmp_path,
        _base_config(security={"password": "never-store-this"}),
    )
    with pytest.raises(LiveConfigurationError, match="must not contain secret value"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )


def test_security_secret_is_resolved_only_from_environment(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    payload = _base_config(
        security={
            "username": "operator",
            "password_env": "PLC_PASSWORD",
            "security_policy": "Basic256Sha256",
            "security_mode": "SignAndEncrypt",
            "client_certificate": "client.der",
            "client_private_key": "client.pem",
            "server_certificate": "server.der",
        }
    )
    path = _write_config(tmp_path, payload)
    loaded = commission.load_commissioning_config(
        path,
        env={"PLC_PASSWORD": "runtime-secret"},
        project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        validate_security_files=False,
    )
    security = loaded.specs[0].connection.security
    assert security.password == "runtime-secret"
    assert security.client_certificate == str((tmp_path / "client.der").resolve())
    assert "runtime-secret" not in repr(security)


def test_missing_secret_environment_variable_fails_closed(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    path = _write_config(
        tmp_path,
        _base_config(
            security={
                "username": "operator",
                "password_env": "MISSING",
                "security_policy": "Basic256Sha256",
                "security_mode": "SignAndEncrypt",
                "client_certificate": "client.der",
                "client_private_key": "client.pem",
                "server_certificate": "server.der",
            }
        ),
    )
    with pytest.raises(LiveConfigurationError, match="MISSING.*not set"):
        commission.load_commissioning_config(
            path,
            env={},
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
            validate_security_files=False,
        )


def test_unknown_schema_fields_are_rejected(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    payload = _base_config()
    payload["secret"] = "x"
    path = _write_config(tmp_path, payload)
    with pytest.raises(LiveConfigurationError, match="unsupported field"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )


def test_required_tag_ids_must_exist_in_canonical_project(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    path = _write_config(tmp_path, _base_config())
    with pytest.raises(LiveConfigurationError, match="were not found"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("OTHER"))),
        )


def test_duplicate_and_unsafe_plc_ids_are_rejected(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    duplicate = {
        "schema": "devagent-live-commission-v1",
        "plcs": [_base_plc(), _base_plc()],
    }
    path = _write_config(tmp_path, duplicate)
    with pytest.raises(LiveConfigurationError, match="Duplicate PLC id"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )

    path = _write_config(tmp_path, _base_config(plc_id="../escape"))
    with pytest.raises(LiveConfigurationError, match="must match"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )


def test_browse_and_tag_bounds_are_enforced(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    path = _write_config(tmp_path, _base_config(browse_max_nodes=5001))
    with pytest.raises(LiveConfigurationError, match="between 1 and 5000"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )

    required = [f"T{i}" for i in range(201)]
    path = _write_config(tmp_path, _base_config(required_tag_ids=required))
    with pytest.raises(LiveConfigurationError, match="at most 200"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(*[_tag(x) for x in required])),
        )


def test_validate_security_files_checks_certificate_paths(tmp_path) -> None:
    (tmp_path / "project.L5X").write_text("fixture", encoding="utf-8")
    path = _write_config(
        tmp_path,
        _base_config(
            security={
                "security_policy": "Basic256Sha256",
                "security_mode": "SignAndEncrypt",
                "client_certificate": "client.der",
                "client_private_key": "client.pem",
                "server_certificate": "server.der",
            }
        ),
    )
    with pytest.raises(LiveConfigurationError, match="file does not exist"):
        commission.load_commissioning_config(
            path,
            project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
        )


def _loaded(tmp_path: Path):
    project_path = tmp_path / "project.L5X"
    project_path.write_text("fixture", encoding="utf-8")
    path = _write_config(tmp_path, _base_config())
    return commission.load_commissioning_config(
        path,
        project_loader=lambda _: SimpleNamespace(project=_project(_tag("TAG:RUN"))),
    )


def _result(state=LiveCommissioningState.COMPLETE, *, evidence=None, reconciliation=None):
    status = SimpleNamespace(
        state=SimpleNamespace(value="CONNECTED"),
        connected=True,
        authentication_mode="ANONYMOUS",
        security_summary="NONE",
    )
    plc = SimpleNamespace(
        state=state,
        connection_status=status,
        reconciliation=reconciliation,
        evidence=evidence,
        error=None,
    )
    return SimpleNamespace(
        started_at=NOW,
        finished_at=NOW,
        all_complete=state is LiveCommissioningState.COMPLETE,
        plc_results={"line1": plc},
    )


def test_summary_contains_no_runtime_secret_fields(tmp_path) -> None:
    loaded = _loaded(tmp_path)
    summary = commission.commissioning_summary(loaded, _result())
    rendered = json.dumps(summary)
    assert summary["mode"] == "READ_ONLY"
    assert "password" not in rendered.casefold()
    assert "private_key" not in rendered.casefold()
    assert summary["plcs"][0]["state"] == "COMPLETE"


def test_artifact_writer_refuses_overwrite_and_hashes_files(tmp_path) -> None:
    loaded = _loaded(tmp_path)
    mapping = SimpleNamespace(
        tag_id="TAG:RUN",
        tag_name="RunCmd",
        tag_scope="controller",
        tag_data_type="BOOL",
        status=SimpleNamespace(value="AUTO_BOUND"),
        reason="exact",
        accepted=True,
        selected_node_id="ns=2;s=RunCmd",
        selected_path="Objects.RunCmd",
        evidence_id="LIVE-MAP:1",
    )
    reconciliation = SimpleNamespace(mappings=(mapping,))
    result = _result(reconciliation=reconciliation)
    out = tmp_path / "artifacts"
    written = commission.write_commissioning_artifacts(out, loaded, result)
    assert written == out.resolve()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "READ_ONLY"
    assert "live_commissioning_summary.json" in manifest["artifacts"]
    assert "line1.tag_reconciliation.json" in manifest["artifacts"]
    with pytest.raises(FileExistsError):
        commission.write_commissioning_artifacts(out, loaded, result)


def test_evidence_artifact_uses_sanitizing_renderer(monkeypatch, tmp_path) -> None:
    loaded = _loaded(tmp_path)
    pack = SimpleNamespace(pack_id="PACK")
    evidence = SimpleNamespace(live_pack=pack)
    result = _result(evidence=evidence)
    monkeypatch.setattr(
        commission,
        "build_live_customer_evidence_artifact",
        lambda supplied: {"pack_id": supplied.pack_id, "raw_excluded_value": False},
    )
    out = tmp_path / "artifacts"
    commission.write_commissioning_artifacts(out, loaded, result)
    payload = json.loads((out / "line1.live_evidence.json").read_text(encoding="utf-8"))
    assert payload == {"pack_id": "PACK", "raw_excluded_value": False}


def test_run_loaded_config_forces_disconnect_when_done(tmp_path) -> None:
    loaded = _loaded(tmp_path)
    calls = []
    expected = _result()

    class Workflow:
        def __init__(self, specs, *, disconnect_when_done):
            calls.append((tuple(specs), disconnect_when_done))

        async def run(self):
            return expected

    result = __import__("asyncio").run(
        commission.run_loaded_commissioning_config(loaded, workflow_factory=Workflow)
    )
    assert result is expected
    assert calls == [(loaded.specs, True)]
