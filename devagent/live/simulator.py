from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LiveConfigurationError, LiveDependencyError
from .security import SUPPORTED_SECURITY_MODES, SUPPORTED_SECURITY_POLICIES
from .simulator_scenarios import SimulatorScenarioSpec, simulator_scenario


def _require_asyncua() -> tuple[Any, Any]:
    try:
        from asyncua import Server, ua
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise LiveDependencyError(
            'The DevAgent Live simulator requires the OPC UA extra. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc
    return Server, ua


def _build_user_manager(expected_username: str, expected_password: str) -> Any:
    try:
        from asyncua.crypto.permission_rules import User, UserRole
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise LiveDependencyError(
            'The DevAgent Live secure simulator requires the OPC UA extra. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc

    class StaticUserManager:
        def get_user(
            self,
            iserver: Any,
            username: str | None = None,
            password: str | None = None,
            certificate: Any = None,
        ) -> Any | None:
            if username is None or password is None:
                return None
            valid_username = hmac.compare_digest(username, expected_username)
            valid_password = hmac.compare_digest(password, expected_password)
            if not (valid_username and valid_password):
                return None
            return User(role=UserRole.User, name=expected_username)

    return StaticUserManager()


@dataclass(frozen=True)
class SimulatorNodeIds:
    auto_mode: str
    start_request: str
    safety_ok: str
    safety_trip: str
    drive_ready: str
    drive_fault: str
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
    """Deterministic OPC UA server used to qualify DevAgent Live.

    ``normal`` preserves the original dynamic transition behavior used by subscription
    and history qualification. The other scenarios are fixed, known-ground-truth
    commissioning states used to measure deterministic diagnosis and System Health.
    """

    NAMESPACE_URI = "urn:devagent:live:simulator"
    APPLICATION_URI = "urn:devagent:live:simulator"

    def __init__(
        self,
        endpoint: str = "opc.tcp://127.0.0.1:4840/devagent/simulator/",
        *,
        scenario: str = "normal",
        update_interval_seconds: float = 0.20,
        username: str | None = None,
        password: str | None = None,
        server_certificate: str | None = None,
        server_private_key: str | None = None,
        server_private_key_password: str | None = None,
        security_policy: str = "Basic256Sha256",
        security_mode: str = "SignAndEncrypt",
    ) -> None:
        scenario_spec = simulator_scenario(scenario)
        if update_interval_seconds <= 0:
            raise ValueError("update_interval_seconds must be > 0")
        if (username is None) != (password is None):
            raise LiveConfigurationError(
                "Simulator username and password must be configured together"
            )
        secure_requested = server_certificate is not None or server_private_key is not None
        if secure_requested and (not server_certificate or not server_private_key):
            raise LiveConfigurationError(
                "Secure simulator mode requires both server certificate and private key"
            )
        if username is not None and not secure_requested:
            raise LiveConfigurationError(
                "Simulator username/password authentication requires SignAndEncrypt"
            )
        if secure_requested:
            if security_policy not in SUPPORTED_SECURITY_POLICIES:
                raise LiveConfigurationError(
                    f"Unsupported simulator security policy {security_policy!r}"
                )
            if security_mode not in SUPPORTED_SECURITY_MODES:
                raise LiveConfigurationError(
                    f"Unsupported simulator security mode {security_mode!r}"
                )
            if username is not None and security_mode != "SignAndEncrypt":
                raise LiveConfigurationError(
                    "Simulator username/password authentication requires SignAndEncrypt"
                )

        self.endpoint = endpoint
        self.scenario = scenario_spec.name
        self.scenario_spec = scenario_spec
        self.update_interval_seconds = update_interval_seconds
        self.username = username
        self._password = password
        self.server_certificate = server_certificate
        self.server_private_key = server_private_key
        self._server_private_key_password = server_private_key_password
        self.security_policy = security_policy
        self.security_mode = security_mode
        self.server: Any | None = None
        self.namespace_index: int | None = None
        self.nodes: dict[str, Any] = {}
        self.node_ids: SimulatorNodeIds | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._tick = 0

    @property
    def secure(self) -> bool:
        return self.server_certificate is not None

    @property
    def scenario_description(self) -> str:
        return self.scenario_spec.description

    @property
    def expected_system_health(self) -> str:
        return self.scenario_spec.expected_system_health

    @property
    def expected_primary_reason(self) -> str:
        return self.scenario_spec.expected_primary_reason

    async def _configure_server_security(self, server: Any, ua: Any) -> None:
        if not self.secure:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
            server.set_identity_tokens([ua.AnonymousIdentityToken])
            return

        assert self.server_certificate is not None
        assert self.server_private_key is not None
        certificate_path = Path(self.server_certificate).expanduser()
        private_key_path = Path(self.server_private_key).expanduser()
        if not certificate_path.is_file():
            raise LiveConfigurationError(
                f"Simulator server certificate file does not exist: {certificate_path}"
            )
        if not private_key_path.is_file():
            raise LiveConfigurationError(
                f"Simulator server private-key file does not exist: {private_key_path}"
            )

        await server.load_certificate(str(certificate_path))
        await server.load_private_key(
            str(private_key_path),
            self._server_private_key_password,
        )
        policy_name = f"{self.security_policy}_{self.security_mode}"
        policy_type = getattr(ua.SecurityPolicyType, policy_name, None)
        if policy_type is None:
            raise LiveConfigurationError(
                f"Installed asyncua does not support simulator security policy {policy_name}"
            )
        server.set_security_policy([policy_type])
        if self.username is not None:
            server.set_identity_tokens([ua.UserNameIdentityToken])
        else:
            server.set_identity_tokens([ua.AnonymousIdentityToken])

    async def start(self) -> None:
        if self.server is not None:
            return
        Server, ua = _require_asyncua()
        user_manager = None
        if self.username is not None:
            assert self._password is not None
            user_manager = _build_user_manager(self.username, self._password)
        server = Server(user_manager=user_manager)
        await server.init()
        await server.set_application_uri(self.APPLICATION_URI)
        server.set_endpoint(self.endpoint)
        server.set_server_name("DevAgent Live OPC UA Simulator")
        await self._configure_server_security(server, ua)
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

        spec = self.scenario_spec
        await add_variable(
            conveyor, "AutoMode", "Warehouse.Conveyor1.AutoMode", spec.auto_mode, ua.VariantType.Boolean
        )
        await add_variable(
            conveyor,
            "StartRequest",
            "Warehouse.Conveyor1.StartRequest",
            spec.start_request,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor, "SafetyOK", "Warehouse.Conveyor1.SafetyOK", spec.safety_ok, ua.VariantType.Boolean
        )
        await add_variable(
            conveyor,
            "SafetyTrip",
            "Warehouse.Conveyor1.SafetyTrip",
            spec.safety_trip,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor,
            "DriveReady",
            "Warehouse.Conveyor1.DriveReady",
            spec.drive_ready,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor,
            "DriveFault",
            "Warehouse.Conveyor1.DriveFault",
            spec.drive_fault,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor,
            "DownstreamReady",
            "Warehouse.Conveyor1.DownstreamReady",
            spec.downstream_ready,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor,
            "RunCmd",
            "Warehouse.Conveyor1.RunCmd",
            spec.run_cmd,
            ua.VariantType.Boolean,
        )
        await add_variable(
            conveyor, "Speed", "Warehouse.Conveyor1.Speed", spec.speed, ua.VariantType.Double
        )
        await add_variable(
            conveyor,
            "FaultCode",
            "Warehouse.Conveyor1.FaultCode",
            spec.fault_code,
            ua.VariantType.Int32,
        )

        await add_variable(
            sorter,
            "ReadyToReceive",
            "Warehouse.Sorter1.ReadyToReceive",
            spec.sorter_ready,
            ua.VariantType.Boolean,
        )
        await add_variable(
            sorter,
            "HomeSensor",
            "Warehouse.Sorter1.HomeSensor",
            spec.home_sensor,
            ua.VariantType.Boolean,
        )

        await add_variable(
            system, "ProductionCount", "Warehouse.System.ProductionCount", 0, ua.VariantType.Int32
        )
        await add_variable(
            system,
            "MachineState",
            "Warehouse.System.MachineState",
            spec.machine_state,
            ua.VariantType.String,
        )
        await add_variable(
            system, "LaneCounts", "Warehouse.System.LaneCounts", [1, 2, 3], ua.VariantType.Int32
        )

        def node_id(key: str) -> str:
            return self.nodes[key].nodeid.to_string()

        self.node_ids = SimulatorNodeIds(
            auto_mode=node_id("AutoMode"),
            start_request=node_id("StartRequest"),
            safety_ok=node_id("SafetyOK"),
            safety_trip=node_id("SafetyTrip"),
            drive_ready=node_id("DriveReady"),
            drive_fault=node_id("DriveFault"),
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
        self._update_task = asyncio.create_task(
            self._run_updates(), name="devagent-live-simulator-updates"
        )

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

    async def _write_fixed_state(self, ua: Any, spec: SimulatorScenarioSpec) -> None:
        values = (
            ("AutoMode", spec.auto_mode, ua.VariantType.Boolean),
            ("StartRequest", spec.start_request, ua.VariantType.Boolean),
            ("SafetyOK", spec.safety_ok, ua.VariantType.Boolean),
            ("SafetyTrip", spec.safety_trip, ua.VariantType.Boolean),
            ("DriveReady", spec.drive_ready, ua.VariantType.Boolean),
            ("DriveFault", spec.drive_fault, ua.VariantType.Boolean),
            ("DownstreamReady", spec.downstream_ready, ua.VariantType.Boolean),
            ("RunCmd", spec.run_cmd, ua.VariantType.Boolean),
            ("Speed", spec.speed, ua.VariantType.Double),
            ("FaultCode", spec.fault_code, ua.VariantType.Int32),
            ("ReadyToReceive", spec.sorter_ready, ua.VariantType.Boolean),
            ("HomeSensor", spec.home_sensor, ua.VariantType.Boolean),
            ("MachineState", spec.machine_state, ua.VariantType.String),
        )
        for key, value, variant_type in values:
            await self.nodes[key].write_value(value, variant_type)

    async def _run_updates(self) -> None:
        _Server, ua = _require_asyncua()
        while True:
            await asyncio.sleep(self.update_interval_seconds)
            self._tick += 1
            spec = self.scenario_spec

            if spec.dynamic:
                # Backward-compatible qualification behavior: create real changes for
                # subscriptions/history while keeping safety/drive health clear.
                ready = (self._tick // 3) % 2 == 0
                await self.nodes["ProductionCount"].write_value(
                    self._tick, ua.VariantType.Int32
                )
                await self.nodes["Speed"].write_value(
                    40.0 + float(self._tick % 10), ua.VariantType.Double
                )
                await self.nodes["DownstreamReady"].write_value(
                    ready, ua.VariantType.Boolean
                )
                await self.nodes["ReadyToReceive"].write_value(
                    ready, ua.VariantType.Boolean
                )
                await self.nodes["HomeSensor"].write_value(
                    ready, ua.VariantType.Boolean
                )
                await self.nodes["RunCmd"].write_value(ready, ua.VariantType.Boolean)
                await self.nodes["MachineState"].write_value(
                    "RUNNING" if ready else "WAITING", ua.VariantType.String
                )
                continue

            # Fixed commissioning scenarios intentionally stay deterministic so an
            # automated evaluator can compare the agent answer with known ground truth.
            await self._write_fixed_state(ua, spec)
            production_count = self._tick if spec.run_cmd else 0
            await self.nodes["ProductionCount"].write_value(
                production_count, ua.VariantType.Int32
            )


__all__ = ["OpcUaSimulator", "SimulatorNodeIds"]
