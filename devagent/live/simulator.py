from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .errors import LiveDependencyError


def _require_asyncua() -> tuple[Any, Any]:
    try:
        from asyncua import Server, ua
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise LiveDependencyError(
            'The DevAgent Live simulator requires the OPC UA extra. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc
    return Server, ua


@dataclass(frozen=True)
class SimulatorNodeIds:
    auto_mode: str
    start_request: str
    drive_ready: str
    downstream_ready: str
    run_cmd: str
    speed: str
    fault_code: str
    sorter_ready: str
    home_sensor: str
    production_count: str
    machine_state: str
    lane_counts: str


class OpcUaSimulator:
    """Deterministic OPC UA server used to qualify DevAgent Live."""

    NAMESPACE_URI = "urn:devagent:live:simulator"

    def __init__(
        self,
        endpoint: str = "opc.tcp://127.0.0.1:4840/devagent/simulator/",
        *,
        scenario: str = "normal",
        update_interval_seconds: float = 0.20,
    ) -> None:
        if scenario not in {"normal", "blocker"}:
            raise ValueError("scenario must be 'normal' or 'blocker'")
        self.endpoint = endpoint
        self.scenario = scenario
        self.update_interval_seconds = update_interval_seconds
        self.server: Any | None = None
        self.namespace_index: int | None = None
        self.nodes: dict[str, Any] = {}
        self.node_ids: SimulatorNodeIds | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._tick = 0

    async def start(self) -> None:
        if self.server is not None:
            return
        Server, ua = _require_asyncua()
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("DevAgent Live OPC UA Simulator")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        namespace_index = await server.register_namespace(self.NAMESPACE_URI)

        warehouse = await server.nodes.objects.add_object(
            ua.NodeId("Warehouse", namespace_index),
            ua.QualifiedName("Warehouse", namespace_index),
        )
        conveyor = await warehouse.add_object(
            ua.NodeId("Warehouse.Conveyor1", namespace_index),
            ua.QualifiedName("Conveyor1", namespace_index),
        )
        sorter = await warehouse.add_object(
            ua.NodeId("Warehouse.Sorter1", namespace_index),
            ua.QualifiedName("Sorter1", namespace_index),
        )
        system = await warehouse.add_object(
            ua.NodeId("Warehouse.System", namespace_index),
            ua.QualifiedName("System", namespace_index),
        )

        async def add_variable(parent: Any, key: str, path: str, value: Any, variant_type: Any) -> Any:
            node = await parent.add_variable(
                ua.NodeId(path, namespace_index),
                ua.QualifiedName(key, namespace_index),
                value,
                varianttype=variant_type,
            )
            await node.set_read_only()
            self.nodes[key] = node
            return node

        await add_variable(conveyor, "AutoMode", "Warehouse.Conveyor1.AutoMode", True, ua.VariantType.Boolean)
        await add_variable(
            conveyor, "StartRequest", "Warehouse.Conveyor1.StartRequest", True, ua.VariantType.Boolean
        )
        await add_variable(conveyor, "DriveReady", "Warehouse.Conveyor1.DriveReady", True, ua.VariantType.Boolean)
        await add_variable(
            conveyor,
            "DownstreamReady",
            "Warehouse.Conveyor1.DownstreamReady",
            self.scenario == "normal",
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor,
            "RunCmd",
            "Warehouse.Conveyor1.RunCmd",
            self.scenario == "normal",
            ua.VariantType.Boolean,
        )
        await add_variable(conveyor, "Speed", "Warehouse.Conveyor1.Speed", 42.5, ua.VariantType.Double)
        await add_variable(conveyor, "FaultCode", "Warehouse.Conveyor1.FaultCode", 0, ua.VariantType.Int32)

        await add_variable(
            sorter,
            "ReadyToReceive",
            "Warehouse.Sorter1.ReadyToReceive",
            self.scenario == "normal",
            ua.VariantType.Boolean,
        )
        await add_variable(
            sorter,
            "HomeSensor",
            "Warehouse.Sorter1.HomeSensor",
            self.scenario == "normal",
            ua.VariantType.Boolean,
        )

        await add_variable(system, "ProductionCount", "Warehouse.System.ProductionCount", 0, ua.VariantType.Int32)
        await add_variable(system, "MachineState", "Warehouse.System.MachineState", "RUNNING", ua.VariantType.String)
        await add_variable(system, "LaneCounts", "Warehouse.System.LaneCounts", [1, 2, 3], ua.VariantType.Int32)

        def node_id(key: str) -> str:
            return self.nodes[key].nodeid.to_string()

        self.node_ids = SimulatorNodeIds(
            auto_mode=node_id("AutoMode"),
            start_request=node_id("StartRequest"),
            drive_ready=node_id("DriveReady"),
            downstream_ready=node_id("DownstreamReady"),
            run_cmd=node_id("RunCmd"),
            speed=node_id("Speed"),
            fault_code=node_id("FaultCode"),
            sorter_ready=node_id("ReadyToReceive"),
            home_sensor=node_id("HomeSensor"),
            production_count=node_id("ProductionCount"),
            machine_state=node_id("MachineState"),
            lane_counts=node_id("LaneCounts"),
        )

        await server.start()
        self.server = server
        self.namespace_index = namespace_index
        self._update_task = asyncio.create_task(self._run_updates(), name="devagent-live-simulator-updates")

    async def stop(self) -> None:
        task, self._update_task = self._update_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        server, self.server = self.server, None
        if server is not None:
            await server.stop()
        self.nodes.clear()

    async def __aenter__(self) -> "OpcUaSimulator":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    async def _run_updates(self) -> None:
        _Server, ua = _require_asyncua()
        while True:
            await asyncio.sleep(self.update_interval_seconds)
            self._tick += 1
            await self.nodes["ProductionCount"].write_value(self._tick, ua.VariantType.Int32)
            await self.nodes["Speed"].write_value(40.0 + float(self._tick % 10), ua.VariantType.Double)

            if self.scenario == "normal":
                # Periodically transition a downstream permissive so subscription
                # qualification sees both the initial value and a real data change.
                ready = (self._tick // 3) % 2 == 0
                await self.nodes["DownstreamReady"].write_value(ready, ua.VariantType.Boolean)
                await self.nodes["ReadyToReceive"].write_value(ready, ua.VariantType.Boolean)
                await self.nodes["HomeSensor"].write_value(ready, ua.VariantType.Boolean)
                await self.nodes["RunCmd"].write_value(ready, ua.VariantType.Boolean)
                await self.nodes["MachineState"].write_value(
                    "RUNNING" if ready else "WAITING", ua.VariantType.String
                )
            else:
                await self.nodes["DownstreamReady"].write_value(False, ua.VariantType.Boolean)
                await self.nodes["ReadyToReceive"].write_value(False, ua.VariantType.Boolean)
                await self.nodes["HomeSensor"].write_value(False, ua.VariantType.Boolean)
                await self.nodes["RunCmd"].write_value(False, ua.VariantType.Boolean)
                await self.nodes["MachineState"].write_value("BLOCKED", ua.VariantType.String)
