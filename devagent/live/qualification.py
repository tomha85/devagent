from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable, Mapping

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .agent_integration import LiveDataTrustLayer, LiveEvidenceDisposition
from .errors import LiveConnectionError, LiveDependencyError
from .manager import MultiPlcConnectionManager, PlcConnectionSpec
from .models import Quality, RuntimeValue, TrustState
from .opcua_client import ReadOnlyOpcUaClient
from .security import LiveSecurityConfig
from .simulator import OpcUaSimulator
from .workflow import LiveCommissioningWorkflow


class LiveQualificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LiveQualificationCase:
    case_id: str
    title: str
    runtime_required: bool


@dataclass(frozen=True)
class LiveQualificationCaseResult:
    case_id: str
    title: str
    status: LiveQualificationStatus
    detail: str
    runtime_required: bool
    duration_seconds: float


@dataclass(frozen=True)
class LiveReleaseQualificationReport:
    started_at: datetime
    finished_at: datetime
    runtime_available: bool
    runtime_version: str | None
    cases: tuple[LiveQualificationCaseResult, ...]

    @property
    def status(self) -> LiveQualificationStatus:
        if any(case.status is LiveQualificationStatus.FAIL for case in self.cases):
            return LiveQualificationStatus.FAIL
        if any(case.status is LiveQualificationStatus.BLOCKED for case in self.cases):
            return LiveQualificationStatus.BLOCKED
        return LiveQualificationStatus.PASS

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and self.status is LiveQualificationStatus.PASS

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(1 for case in self.cases if case.status is status)
            for status in LiveQualificationStatus
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-release-qualification-v1",
            "mode": "READ_ONLY",
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "runtime": {
                "asyncua_available": self.runtime_available,
                "asyncua_version": self.runtime_version,
            },
            "status": self.status.value,
            "counts": self.counts(),
            "cases": [
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "status": case.status.value,
                    "detail": case.detail,
                    "runtime_required": case.runtime_required,
                    "duration_seconds": round(case.duration_seconds, 6),
                }
                for case in self.cases
            ],
        }


@dataclass
class _SecureMaterials:
    server_key: Path
    server_cert: Path
    wrong_server_cert: Path
    client_key: Path
    client_cert: Path
    client_uri: str


@dataclass
class _QualificationContext:
    workdir: Path
    secure_materials: _SecureMaterials | None = None


CaseRunner = Callable[[_QualificationContext], Awaitable[str | None]]

_QUALIFICATION_PASSWORD = "devagent-qualification-password"
_WRONG_PASSWORD = "devagent-wrong-password"
_QUALIFICATION_SECRETS = (_QUALIFICATION_PASSWORD, _WRONG_PASSWORD)


LIVE_RELEASE_QUALIFICATION_CASES: tuple[LiveQualificationCase, ...] = (
    LiveQualificationCase("LQ-001", "Read-only public surface", False),
    LiveQualificationCase("LQ-002", "Deterministic live trust gate", False),
    LiveQualificationCase("LQ-003", "Unreachable endpoint fails closed", True),
    LiveQualificationCase("LQ-004", "Anonymous typed read path", True),
    LiveQualificationCase("LQ-005", "BAD node is untrusted", True),
    LiveQualificationCase("LQ-006", "Subscription observes live changes", True),
    LiveQualificationCase("LQ-007", "Reconnect restores active subscription", True),
    LiveQualificationCase("LQ-008", "Secure username/password succeeds", True),
    LiveQualificationCase("LQ-009", "Wrong password is rejected and redacted", True),
    LiveQualificationCase("LQ-010", "Wrong pinned server certificate is rejected", True),
    LiveQualificationCase("LQ-011", "Anonymous login is rejected by authenticated server", True),
    LiveQualificationCase("LQ-012", "Three PLC sessions connect and read independently", True),
    LiveQualificationCase("LQ-013", "One PLC outage is isolated from healthy PLCs", True),
    LiveQualificationCase("LQ-014", "Runtime browse surface remains read-only", True),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(text: str) -> str:
    redacted = str(text)
    for secret in _QUALIFICATION_SECRETS:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _runtime_info() -> tuple[bool, str | None]:
    if importlib.util.find_spec("asyncua") is None:
        return False, None
    try:
        return True, importlib.metadata.version("asyncua")
    except importlib.metadata.PackageNotFoundError:
        return True, None


def _free_endpoint(path: str = "devagent/qualification/") -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/{path.lstrip('/')}"


def _runtime_value(
    *,
    quality: Quality = Quality.GOOD,
    stale: bool = False,
    replayed: bool = False,
) -> RuntimeValue:
    now = _now()
    return RuntimeValue(
        node_id="ns=2;s=Qualification.Value",
        value=True,
        variant_type="Boolean",
        status_code="Good" if quality is Quality.GOOD else quality.value,
        quality=quality,
        source_timestamp=now,
        server_timestamp=now,
        received_at=now,
        age_seconds=10.0 if stale else 0.0,
        stale=stale,
        replayed=replayed,
    )


async def _case_read_only_surface(_context: _QualificationContext) -> str:
    prohibited = (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    )
    targets = (
        ReadOnlyOpcUaClient,
        MultiPlcConnectionManager,
        LiveCommissioningWorkflow,
    )
    exposed = [
        f"{target.__name__}.{name}"
        for target in targets
        for name in prohibited
        if hasattr(target, name)
    ]
    if exposed:
        raise AssertionError("write/control surface exposed: " + ", ".join(exposed))
    return "Client, manager, and commissioning workflow expose no PLC control methods"


async def _case_trust_gate(_context: _QualificationContext) -> str:
    layer = LiveDataTrustLayer()
    samples = (
        (_runtime_value(), LiveEvidenceDisposition.CURRENT),
        (_runtime_value(stale=True), LiveEvidenceDisposition.STALE),
        (_runtime_value(quality=Quality.UNCERTAIN), LiveEvidenceDisposition.UNCERTAIN),
        (_runtime_value(quality=Quality.BAD), LiveEvidenceDisposition.UNTRUSTED),
        (_runtime_value(replayed=True), LiveEvidenceDisposition.REPLAYED),
    )
    for value, expected in samples:
        actual = layer.classify(value)
        if actual is not expected:
            raise AssertionError(f"trust classification mismatch: expected={expected.value} actual={actual.value}")
        record = layer.record(plc_id="qualification", plc_name="Qualification", value=value)
        should_be_eligible = expected is LiveEvidenceDisposition.CURRENT
        if record.agent_eligible is not should_be_eligible:
            raise AssertionError(f"agent eligibility mismatch for {expected.value}")
        if record.definitive_current is not should_be_eligible:
            raise AssertionError(f"definitive-current mismatch for {expected.value}")
    return "Only GOOD/CURRENT/non-stale/non-replayed data is agent-eligible"


async def _case_unreachable(_context: _QualificationContext) -> str:
    client = ReadOnlyOpcUaClient(_free_endpoint(), timeout_seconds=0.25, auto_reconnect=False)
    try:
        await client.discover_endpoints()
    except LiveConnectionError:
        return "Unreachable endpoint rejected with LiveConnectionError"
    raise AssertionError("unreachable endpoint unexpectedly reported reachable")


async def _case_anonymous_typed(_context: _QualificationContext) -> str:
    endpoint = _free_endpoint()
    async with OpcUaSimulator(endpoint, scenario="blocker") as simulator:
        assert simulator.node_ids is not None
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        await client.connect()
        try:
            values = (
                await client.read(simulator.node_ids.run_cmd),
                await client.read(simulator.node_ids.speed),
                await client.read(simulator.node_ids.fault_code),
                await client.read(simulator.node_ids.machine_state),
                await client.read(simulator.node_ids.lane_counts),
            )
            expected_types = ("Boolean", "Double", "Int32", "String", "Int32")
            if tuple(value.variant_type for value in values) != expected_types:
                raise AssertionError("typed OPC UA values did not preserve expected variant types")
            if any(value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT for value in values):
                raise AssertionError("anonymous typed read returned non-current data")
            return "Boolean/Double/Int32/String/array reads are GOOD and CURRENT"
        finally:
            await client.disconnect()


async def _case_bad_node(_context: _QualificationContext) -> str:
    endpoint = _free_endpoint()
    async with OpcUaSimulator(endpoint, scenario="normal") as simulator:
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        await client.connect()
        try:
            value = await client.read(f"ns={simulator.namespace_index};s=Qualification.DoesNotExist")
            if value.quality is not Quality.BAD or value.trust is not TrustState.UNTRUSTED:
                raise AssertionError("missing node did not fail closed as BAD/UNTRUSTED")
            if value.loaded_successfully:
                raise AssertionError("BAD node was incorrectly marked loaded successfully")
            return "Missing node represented as BAD/UNTRUSTED without raising into a false success"
        finally:
            await client.disconnect()


async def _case_subscription(_context: _QualificationContext) -> str:
    endpoint = _free_endpoint()
    async with OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=0.05) as simulator:
        assert simulator.node_ids is not None
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        await client.connect()
        try:
            changes = await client.collect_changes(
                [simulator.node_ids.run_cmd],
                count=2,
                timeout_seconds=2.0,
                publishing_interval_ms=50.0,
                sampling_interval_ms=20.0,
            )
            if len(changes) != 2:
                raise AssertionError(f"expected 2 changes, received {len(changes)}")
            if any(change.quality is not Quality.GOOD for change in changes):
                raise AssertionError("subscription delivered non-GOOD data")
            if len({change.value for change in changes}) < 2:
                raise AssertionError("subscription did not observe an actual value transition")
            return "Subscription received initial/current data and a real value transition"
        finally:
            await client.disconnect()


async def _case_reconnect(_context: _QualificationContext) -> str:
    endpoint = _free_endpoint()
    first = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=1.0)
    replacement: OpcUaSimulator | None = None
    collect_task: asyncio.Task[list[Any]] | None = None
    client = ReadOnlyOpcUaClient(
        endpoint,
        timeout_seconds=0.25,
        auto_reconnect=True,
        reconnect_max_delay_seconds=0.25,
        reconnect_request_timeout_seconds=5.0,
    )
    await first.start()
    assert first.node_ids is not None
    node_id = first.node_ids.production_count
    await client.connect()
    try:
        collect_task = asyncio.create_task(
            client.collect_changes(
                [node_id],
                count=3,
                timeout_seconds=10.0,
                publishing_interval_ms=50.0,
                sampling_interval_ms=20.0,
            )
        )
        await asyncio.sleep(0.40)
        if collect_task.done():
            raise AssertionError("subscription unexpectedly completed before outage")
        await first.stop()

        deadline = asyncio.get_running_loop().time() + 3.0
        while client.connection_state == "CONNECTED" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if client.connection_state not in {"DISCONNECTED", "RECONNECTING"}:
            raise AssertionError(f"client did not expose outage state: {client.connection_state}")

        replacement = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=0.05)
        await replacement.start()
        assert replacement.node_ids is not None
        if replacement.node_ids.production_count != node_id:
            raise AssertionError("replacement simulator changed deterministic NodeId")

        await client.wait_until_connected(timeout_seconds=5.0)
        changes = await asyncio.wait_for(collect_task, timeout=5.0)
        if len(changes) != 3 or any(change.quality is not Quality.GOOD for change in changes):
            raise AssertionError("pre-outage subscription was not restored with GOOD data")
        recovered = await client.read(node_id)
        if recovered.quality is not Quality.GOOD or recovered.trust is not TrustState.CURRENT:
            raise AssertionError("fresh post-reconnect read is not GOOD/CURRENT")
        return "Existing subscription recovered after server restart and fresh read is GOOD/CURRENT"
    finally:
        if collect_task is not None and not collect_task.done():
            collect_task.cancel()
            try:
                await collect_task
            except asyncio.CancelledError:
                pass
        await client.disconnect()
        if replacement is not None:
            await replacement.stop()
        await first.stop()


def _create_application_certificate(
    private_key_path: Path,
    certificate_path: Path,
    application_uri: str,
    *,
    server: bool,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _now()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Qualification"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                "DevAgent Secure Simulator" if server else "DevAgent Secure Client",
            ),
        ]
    )
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(application_uri),
                    x509.DNSName(socket.gethostname()),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .sign(key, hashes.SHA256())
    )
    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.DER))


def _secure_materials(context: _QualificationContext) -> _SecureMaterials:
    if context.secure_materials is not None:
        return context.secure_materials
    server_key = context.workdir / "server-key.pem"
    server_cert = context.workdir / "server-cert.der"
    wrong_server_key = context.workdir / "wrong-server-key.pem"
    wrong_server_cert = context.workdir / "wrong-server-cert.der"
    client_key = context.workdir / "client-key.pem"
    client_cert = context.workdir / "client-cert.der"
    client_uri = "urn:devagent:qualification:secure-client"
    _create_application_certificate(server_key, server_cert, OpcUaSimulator.APPLICATION_URI, server=True)
    _create_application_certificate(
        wrong_server_key,
        wrong_server_cert,
        "urn:devagent:qualification:wrong-server",
        server=True,
    )
    _create_application_certificate(client_key, client_cert, client_uri, server=False)
    context.secure_materials = _SecureMaterials(
        server_key=server_key,
        server_cert=server_cert,
        wrong_server_cert=wrong_server_cert,
        client_key=client_key,
        client_cert=client_cert,
        client_uri=client_uri,
    )
    return context.secure_materials


def _secure_client(
    endpoint: str,
    materials: _SecureMaterials,
    *,
    password: str = _QUALIFICATION_PASSWORD,
    server_certificate: Path | None = None,
) -> ReadOnlyOpcUaClient:
    return ReadOnlyOpcUaClient(
        endpoint,
        auto_reconnect=False,
        security=LiveSecurityConfig(
            username="operator",
            password=password,
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=str(materials.client_cert),
            client_private_key=str(materials.client_key),
            server_certificate=str(server_certificate or materials.server_cert),
            application_uri=materials.client_uri,
        ),
    )


async def _with_secure_server(
    context: _QualificationContext,
    action: Callable[[str, OpcUaSimulator, _SecureMaterials], Awaitable[str | None]],
) -> str | None:
    materials = _secure_materials(context)
    endpoint = _free_endpoint("devagent/secure-qualification/")
    async with OpcUaSimulator(
        endpoint,
        scenario="blocker",
        username="operator",
        password=_QUALIFICATION_PASSWORD,
        server_certificate=str(materials.server_cert),
        server_private_key=str(materials.server_key),
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
    ) as simulator:
        return await action(endpoint, simulator, materials)


async def _case_secure_good(context: _QualificationContext) -> str:
    async def action(endpoint: str, simulator: OpcUaSimulator, materials: _SecureMaterials) -> str:
        assert simulator.node_ids is not None
        client = _secure_client(endpoint, materials)
        await client.connect()
        try:
            value = await client.read(simulator.node_ids.machine_state)
            if value.value != "BLOCKED" or value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT:
                raise AssertionError("secure authenticated read did not return expected GOOD/CURRENT value")
            if client.authentication_mode != "USERNAME_PASSWORD":
                raise AssertionError("secure client did not report username/password authentication")
            if client.security_summary != "Basic256Sha256/SignAndEncrypt":
                raise AssertionError("secure client did not report expected channel policy")
            return "Pinned Basic256Sha256/SignAndEncrypt username/password session reads GOOD/CURRENT data"
        finally:
            await client.disconnect()

    return str(await _with_secure_server(context, action))


async def _case_wrong_password(context: _QualificationContext) -> str:
    async def action(endpoint: str, _simulator: OpcUaSimulator, materials: _SecureMaterials) -> str:
        client = _secure_client(endpoint, materials, password=_WRONG_PASSWORD)
        try:
            await client.connect()
        except LiveConnectionError as exc:
            rendered = str(exc)
            if _WRONG_PASSWORD in rendered:
                raise AssertionError("wrong password leaked in connection error")
            if exc.__cause__ is not None:
                raise AssertionError("raw authentication exception chain was preserved")
            return "Wrong password rejected without exposing the credential"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("wrong password unexpectedly authenticated")

    return str(await _with_secure_server(context, action))


async def _case_wrong_pin(context: _QualificationContext) -> str:
    async def action(endpoint: str, _simulator: OpcUaSimulator, materials: _SecureMaterials) -> str:
        client = _secure_client(endpoint, materials, server_certificate=materials.wrong_server_cert)
        try:
            await client.connect()
        except LiveConnectionError:
            return "Server certificate mismatch rejected before qualification could accept the session"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("wrong server certificate pin unexpectedly connected")

    return str(await _with_secure_server(context, action))


async def _case_anonymous_rejected(context: _QualificationContext) -> str:
    async def action(endpoint: str, _simulator: OpcUaSimulator, _materials: _SecureMaterials) -> str:
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        try:
            await client.connect()
        except LiveConnectionError:
            return "Authenticated secure server rejected anonymous client"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("anonymous client unexpectedly authenticated")

    return str(await _with_secure_server(context, action))


async def _start_three() -> tuple[list[OpcUaSimulator], dict[str, str]]:
    simulators = [
        OpcUaSimulator(_free_endpoint(f"devagent/qualification/{name}/"), scenario="normal", update_interval_seconds=0.05)
        for name in ("a", "b", "c")
    ]
    try:
        for simulator in simulators:
            await simulator.start()
        node_ids: dict[str, str] = {}
        for name, simulator in zip(("a", "b", "c"), simulators):
            assert simulator.node_ids is not None
            node_ids[name] = simulator.node_ids.production_count
        return simulators, node_ids
    except Exception:
        for simulator in reversed(simulators):
            await simulator.stop()
        raise


def _three_manager(simulators: list[OpcUaSimulator]) -> MultiPlcConnectionManager:
    return MultiPlcConnectionManager(
        [
            PlcConnectionSpec(name, simulator.endpoint, auto_reconnect=False)
            for name, simulator in zip(("a", "b", "c"), simulators)
        ]
    )


async def _case_three_plc(_context: _QualificationContext) -> str:
    simulators, node_ids = await _start_three()
    manager = _three_manager(simulators)
    try:
        statuses = await manager.connect_all()
        if any(not status.connected for status in statuses.values()):
            raise AssertionError("not all three PLC sessions connected")
        reads = await manager.read_many({name: [node_ids[name]] for name in node_ids})
        if any(not result.succeeded for result in reads.values()):
            raise AssertionError("one or more healthy PLC reads failed")
        for result in reads.values():
            if len(result.values) != 1 or result.values[0].quality is not Quality.GOOD or result.values[0].trust is not TrustState.CURRENT:
                raise AssertionError("three-PLC read did not produce exactly one GOOD/CURRENT value per PLC")
        return "Three independent PLC sessions connected and returned GOOD/CURRENT values"
    finally:
        await manager.disconnect_all()
        for simulator in reversed(simulators):
            await simulator.stop()


async def _case_failure_isolation(_context: _QualificationContext) -> str:
    simulators, node_ids = await _start_three()
    manager = _three_manager(simulators)
    try:
        statuses = await manager.connect_all()
        if any(not status.connected for status in statuses.values()):
            raise AssertionError("initial three-PLC connection did not succeed")
        await simulators[1].stop()
        reads = await manager.read_many({name: [node_ids[name]] for name in node_ids})
        if reads["b"].succeeded:
            raise AssertionError("outage PLC B unexpectedly read successfully")
        for name in ("a", "c"):
            result = reads[name]
            if not result.succeeded or len(result.values) != 1:
                raise AssertionError(f"healthy PLC {name.upper()} was disrupted by PLC B outage")
            value = result.values[0]
            if value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT:
                raise AssertionError(f"healthy PLC {name.upper()} lost GOOD/CURRENT trust")
        return "PLC B outage produced an isolated error while PLC A/C remained GOOD/CURRENT"
    finally:
        await manager.disconnect_all()
        for simulator in reversed(simulators):
            await simulator.stop()


async def _case_runtime_browse_read_only(_context: _QualificationContext) -> str:
    endpoint = _free_endpoint()
    async with OpcUaSimulator(endpoint, scenario="normal"):
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        await client.connect()
        try:
            nodes = await client.browse(max_depth=4, max_nodes=100)
            variables = [node for node in nodes if node.node_class == "Variable"]
            if not variables:
                raise AssertionError("qualification browse found no Variable nodes")
            if any(node.writable for node in variables):
                raise AssertionError("qualification simulator exposed a writable Variable")
            if any(not node.readable for node in variables):
                raise AssertionError("qualification simulator exposed unreadable test Variables")
            return f"Browsed {len(variables)} Variable nodes; all are readable and non-writable"
        finally:
            await client.disconnect()


_DEFAULT_RUNNERS: dict[str, CaseRunner] = {
    "LQ-001": _case_read_only_surface,
    "LQ-002": _case_trust_gate,
    "LQ-003": _case_unreachable,
    "LQ-004": _case_anonymous_typed,
    "LQ-005": _case_bad_node,
    "LQ-006": _case_subscription,
    "LQ-007": _case_reconnect,
    "LQ-008": _case_secure_good,
    "LQ-009": _case_wrong_password,
    "LQ-010": _case_wrong_pin,
    "LQ-011": _case_anonymous_rejected,
    "LQ-012": _case_three_plc,
    "LQ-013": _case_failure_isolation,
    "LQ-014": _case_runtime_browse_read_only,
}


async def run_live_release_qualification(
    *,
    runtime_available: bool | None = None,
    runtime_version: str | None = None,
    runner_overrides: Mapping[str, CaseRunner] | None = None,
) -> LiveReleaseQualificationReport:
    detected_available, detected_version = _runtime_info()
    available = detected_available if runtime_available is None else bool(runtime_available)
    version = detected_version if runtime_version is None else runtime_version
    started_at = _now()
    results: list[LiveQualificationCaseResult] = []
    runners = dict(_DEFAULT_RUNNERS)
    if runner_overrides:
        unknown = sorted(set(runner_overrides) - {case.case_id for case in LIVE_RELEASE_QUALIFICATION_CASES})
        if unknown:
            raise ValueError("Unknown qualification case override(s): " + ", ".join(unknown))
        runners.update(runner_overrides)

    with TemporaryDirectory(prefix="devagent-live-qualification-") as temp_dir:
        context = _QualificationContext(workdir=Path(temp_dir))
        for case in LIVE_RELEASE_QUALIFICATION_CASES:
            start = time.monotonic()
            if case.runtime_required and not available:
                results.append(
                    LiveQualificationCaseResult(
                        case_id=case.case_id,
                        title=case.title,
                        status=LiveQualificationStatus.BLOCKED,
                        detail='asyncua runtime unavailable; install with: python -m pip install "devagent-ai[live]"',
                        runtime_required=True,
                        duration_seconds=time.monotonic() - start,
                    )
                )
                continue
            runner = runners[case.case_id]
            try:
                detail = await runner(context)
            except LiveDependencyError as exc:
                status = LiveQualificationStatus.BLOCKED
                rendered = _redact(str(exc))
            except Exception as exc:
                status = LiveQualificationStatus.FAIL
                rendered = _redact(f"{type(exc).__name__}: {exc}")
            else:
                status = LiveQualificationStatus.PASS
                rendered = _redact(detail or "Qualification case passed")
            results.append(
                LiveQualificationCaseResult(
                    case_id=case.case_id,
                    title=case.title,
                    status=status,
                    detail=rendered,
                    runtime_required=case.runtime_required,
                    duration_seconds=time.monotonic() - start,
                )
            )

    return LiveReleaseQualificationReport(
        started_at=started_at,
        finished_at=_now(),
        runtime_available=available,
        runtime_version=version if available else None,
        cases=tuple(results),
    )


def write_live_release_qualification_artifacts(
    output_dir: Path,
    report: LiveReleaseQualificationReport,
) -> Path:
    destination = Path(output_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=False)
    report_path = destination / "live_release_qualification.json"
    manifest_path = destination / "manifest.json"
    payload = json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    manifest = {
        "schema": "devagent-live-release-qualification-manifest-v1",
        "mode": "READ_ONLY",
        "qualification_status": report.status.value,
        "artifacts": {
            report_path.name: {
                "sha256": digest,
                "bytes": len(payload.encode("utf-8")),
            }
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return destination


__all__ = [
    "LIVE_RELEASE_QUALIFICATION_CASES",
    "LiveQualificationCase",
    "LiveQualificationCaseResult",
    "LiveQualificationStatus",
    "LiveReleaseQualificationReport",
    "run_live_release_qualification",
    "write_live_release_qualification_artifacts",
]
