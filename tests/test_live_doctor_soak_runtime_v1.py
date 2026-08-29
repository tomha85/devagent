from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devagent.live.doctor import LiveDoctorStatus, run_live_doctor
from devagent.live.manager import PlcSessionState
from devagent.live.models import Quality, RuntimeValue
from devagent.live.runtime_environment import LiveOpcUaRuntimeStatus
from devagent.live.security import LiveSecurityConfig
from devagent.live.soak import LiveSoakStatus, run_live_soak


def test_doctor_can_reach_exact_eight_pass_checks(monkeypatch, tmp_path):
    import devagent.live.doctor as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(True, "2.0.1", True, "supported asyncua"),
    )

    def version(package: str):
        return {"cryptography": "46.0.0", "devagent-ai": "0.8.7"}.get(package)

    monkeypatch.setattr(module, "_version", version)
    monkeypatch.setattr(
        module,
        "load_live_engineering_context",
        lambda _path: SimpleNamespace(
            context=SimpleNamespace(
                vendor="ROCKWELL",
                controller_name="Line1",
                tags=(1, 2),
                rules=(1,),
            )
        ),
    )

    class FakeClient:
        def __init__(self, endpoint, *, security, auto_reconnect):
            assert endpoint.startswith("opc.tcp://")
            assert auto_reconnect is False
            self.authentication_mode = security.authentication_mode
            self.security_summary = security.channel_summary

        async def discover_endpoints(self):
            return (object(),)

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    monkeypatch.setattr(module, "ReadOnlyOpcUaClient", FakeClient)

    report = asyncio.run(
        run_live_doctor(
            project_path=tmp_path / "warehouse.L5X",
            endpoint="opc.tcp://127.0.0.1:4840/",
            security=LiveSecurityConfig(),
            output_parent=tmp_path,
        )
    )

    assert report.status is LiveDoctorStatus.PASS
    assert [item.check_id for item in report.checks] == [f"DR-{index:03d}" for index in range(1, 9)]
    assert all(item.status is LiveDoctorStatus.PASS for item in report.checks)


def _runtime_value(node_id: str = "ns=2;s=Value") -> RuntimeValue:
    now = datetime.now(timezone.utc)
    return RuntimeValue(
        node_id=node_id,
        value=True,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=now,
        server_timestamp=now,
        received_at=now,
        age_seconds=0.0,
        stale=False,
        replayed=False,
    )


def test_short_deterministic_soak_control_flow_passes_with_current_values(monkeypatch):
    import devagent.live.soak as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(True, "2.0.1", True, "supported"),
    )

    class FakeStatus:
        connected = True
        state = PlcSessionState.CONNECTED
        last_error = None

    class FakeManager:
        def __init__(self, connections):
            self._ids = tuple(item.plc_id for item in connections)

        @property
        def plc_ids(self):
            return self._ids

        async def connect_all(self):
            return {plc_id: FakeStatus() for plc_id in self._ids}

        def status(self, plc_id):
            assert plc_id in self._ids
            return FakeStatus()

        async def read_many(self, request):
            return {
                plc_id: SimpleNamespace(
                    error=None,
                    values=tuple(_runtime_value(node) for node in node_ids),
                    state=PlcSessionState.CONNECTED,
                )
                for plc_id, node_ids in request.items()
            }

        async def disconnect_all(self):
            return {plc_id: FakeStatus() for plc_id in self._ids}

    monkeypatch.setattr(module, "MultiPlcConnectionManager", FakeManager)

    async def reconcile(_manager, plc_id, _project, **_kwargs):
        return SimpleNamespace(
            node_request_map=lambda **_options: {plc_id: ("ns=2;s=Value",)}
        )

    monkeypatch.setattr(module, "reconcile_connected_project_tags", reconcile)

    security = LiveSecurityConfig()
    connection = SimpleNamespace(
        plc_id="p1",
        display_name="PLC 1",
        security=security,
    )
    spec = SimpleNamespace(
        connection=connection,
        engineering_project=SimpleNamespace(tags=(1,)),
        explicit_node_map={},
        browse_max_depth=4,
        browse_max_nodes=100,
        required_tag_ids=("T1",),
        require_all_mappings=True,
    )
    config = SimpleNamespace(specs=(spec,))

    report = asyncio.run(
        run_live_soak(
            config,
            duration_seconds=0.1,
            interval_seconds=0.01,
            min_current_ratio=1.0,
            max_consecutive_error_cycles=0,
            max_memory_growth_mb=1024.0,
        )
    )

    assert report.status is LiveSoakStatus.PASS
    assert report.runtime_version == "2.0.1"
    assert len(report.plcs) == 1
    assert report.plcs[0].cycles >= 1
    assert report.plcs[0].current_ratio == 1.0
    assert report.plcs[0].final_state == "CONNECTED"
