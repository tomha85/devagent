from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from devagent.live.commission import LoadedCommissioningConfig, commissioning_summary
from devagent.live.manager import PlcConnectionSpec
from devagent.live.security import LiveSecurityConfig
from devagent.live.workflow import LiveCommissioningState


def test_summary_redacts_upstream_error_even_if_workflow_regresses() -> None:
    secret = "runtime-plc-secret"
    security = LiveSecurityConfig(
        username="operator",
        password=secret,
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate="client.der",
        client_private_key="client.pem",
        server_certificate="server.der",
    )
    connection = PlcConnectionSpec(
        "line1",
        "opc.tcp://127.0.0.1:4840/",
        security=security,
    )
    spec = SimpleNamespace(
        connection=connection,
        engineering_project=SimpleNamespace(),
        required_tag_ids=("TAG:RUN",),
    )
    config = LoadedCommissioningConfig(Path("commission.json"), "config-sha", (spec,))
    status = SimpleNamespace(
        state=SimpleNamespace(value="DEGRADED"),
        connected=False,
        authentication_mode="USERNAME_PASSWORD",
        security_summary="Basic256Sha256/SignAndEncrypt",
    )
    plc_result = SimpleNamespace(
        state=LiveCommissioningState.CAPTURE_FAILED,
        connection_status=status,
        reconciliation=None,
        evidence=None,
        error=f"read failed with password {secret}",
    )
    now = datetime(2026, 8, 29, 4, 30, tzinfo=timezone.utc)
    result = SimpleNamespace(
        started_at=now,
        finished_at=now,
        all_complete=False,
        plc_results={"line1": plc_result},
    )

    summary = commissioning_summary(config, result)
    error = summary["plcs"][0]["error"]
    assert secret not in error
    assert "<redacted>" in error
