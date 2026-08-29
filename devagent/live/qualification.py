from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
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
        if any(item.status is LiveQualificationStatus.FAIL for item in self.cases):
            return LiveQualificationStatus.FAIL
        if any(item.status is LiveQualificationStatus.BLOCKED for item in self.cases):
            return LiveQualificationStatus.BLOCKED
        return LiveQualificationStatus.PASS

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and self.status is LiveQualificationStatus.PASS

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(item.status is status for item in self.cases)
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
                    "case_id": item.case_id,
                    "title": item.title,
                    "status": item.status.value,
                    "detail": _redact(item.detail),
                    "runtime_required": item.runtime_required,
                    "duration_seconds": round(item.duration_seconds, 6),
                }
                for item in self.cases
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
class _Context:
    workdir: Path
    secure: _SecureMaterials | None = None


CaseRunner = Callable[[_Context], Awaitable[str | None]]
_PASSWORD = "devagent-qualification-password"
_WRONG_PASSWORD = "devagent-wrong-password"
_SECRETS = (_PASSWORD, _WRONG_PASSWORD)

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
    rendered = str(text)
    for secret in _SECRETS:
        rendered = rendered.replace(secret, "<redacted>")
    return rendered


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


def _value(*, quality: Quality = Quality.GOOD, stale: bool = False, replayed: bool = False) -> RuntimeValue:
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


async def _read_only_surface(_ctx: _Context) -> str:
    prohibited = ("write", "write_value", "set_value", "call_method", "force", "reset", "download", "change_mode")
    exposed = [
        f"{target.__name__}.{name}"
        for target in (ReadOnlyOpcUaClient, MultiPlcConnectionManager, LiveCommissioningWorkflow)
        for name in prohibited
        if hasattr(target, name)
    ]
    if exposed:
        raise AssertionError("write/control surface exposed: " + ", ".join(exposed))
    return "Client, manager, and commissioning workflow expose no PLC control methods"


async def _trust_gate(_ctx: _Context) -> str:
    layer = LiveDataTrustLayer()
    samples = (
        (_value(), LiveEvidenceDisposition.CURRENT),
        (_value(stale=True), LiveEvidenceDisposition.STALE),
        (_value(quality=Quality.UNCERTAIN), LiveEvidenceDisposition.UNCERTAIN),
        (_value(quality=Quality.BAD), LiveEvidenceDisposition.UNTRUSTED),
        (_value(replayed=True), LiveEvidenceDisposition.REPLAYED),
    )
    for value, expected in samples:
        if layer.classify(value) is not expected:
            raise AssertionError(f"trust classification mismatch for {expected.value}")
        record = layer.record(plc_id="qualification", plc_name="Qualification", value=value)
        eligible = expected is LiveEvidenceDisposition.CURRENT
        if record.agent_eligible is not eligible or record.definitive_current is not eligible:
            raise AssertionError(f"trust eligibility mismatch for {expected.value}")
    return "Only GOOD/CURRENT/non-stale/non-replayed data is agent-eligible"


async def _unreachable(_ctx: _Context) -> str:
    client = ReadOnlyOpcUaClient(_free_endpoint(), timeout_seconds=0.25, auto_reconnect=False)
    try:
        await client.discover_endpoints()
    except LiveConnectionError:
        return "Unreachable endpoint rejected with LiveConnectionError"
    raise AssertionError("unreachable endpoint unexpectedly reported reachable")


async def _anonymous_typed(_ctx: _Context) -> str:
    async with OpcUaSimulator(_free_endpoint(), scenario="blocker") as simulator:
        assert simulator.node_ids is not None
        client = ReadOnlyOpcUaClient(simulator.endpoint, auto_reconnect=False)
        await client.connect()
        try:
            values = (
                await client.read(simulator.node_ids.run_cmd),
                await client.read(simulator.node_ids.speed),
                await client.read(simulator.node_ids.fault_code),
                await client.read(simulator.node_ids.machine_state),
                await client.read(simulator.node_ids.lane_counts),
            )
            if tuple(item.variant_type for item in values) != ("Boolean", "Double", "Int32", "String", "Int32"):
                raise AssertionError("typed OPC UA values did not preserve expected variant types")
            if any(item.quality is not Quality.GOOD or item.trust is not TrustState.CURRENT for item in values):
                raise AssertionError("anonymous typed read returned non-current data")
            return "Boolean/Double/Int32/String/array reads are GOOD and CURRENT"
        finally:
            await client.disconnect()


async def _bad_node(_ctx: _Context) -> str:
    async with OpcUaSimulator(_free_endpoint(), scenario="normal") as simulator:
        client = ReadOnlyOpcUaClient(simulator.endpoint, auto_reconnect=False)
        await client.connect()
        try:
            value = await client.read(f"ns={simulator.namespace_index};s=Qualification.DoesNotExist")
            if value.quality is not Quality.BAD or value.trust is not TrustState.UNTRUSTED or value.loaded_successfully:
                raise AssertionError("missing node did not fail closed as BAD/UNTRUSTED")
            return "Missing node represented as BAD/UNTRUSTED without false success"
        finally:
            await client.disconnect()


async def _subscription(_ctx: _Context) -> str:
    async with OpcUaSimulator(_free_endpoint(), scenario="normal", update_interval_seconds=0.05) as simulator:
        assert simulator.node_ids is not None
        client = ReadOnlyOpcUaClient(simulator.endpoint, auto_reconnect=False)
        await client.connect()
        try:
            changes = await client.collect_changes(
                [simulator.node_ids.run_cmd], count=2, timeout_seconds=2.0,
                publishing_interval_ms=50.0, sampling_interval_ms=20.0,
            )
            if len(changes) != 2 or any(item.quality is not Quality.GOOD for item in changes):
                raise AssertionError("subscription did not return two GOOD observations")
            if len({item.value for item in changes}) < 2:
                raise AssertionError("subscription did not observe an actual transition")
            return "Subscription received initial/current data and a real value transition"
        finally:
            await client.disconnect()


async def _reconnect(_ctx: _Context) -> str:
    endpoint = _free_endpoint()
    first = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=1.0)
    replacement: OpcUaSimulator | None = None
    task: asyncio.Task[list[Any]] | None = None
    client = ReadOnlyOpcUaClient(
        endpoint, timeout_seconds=0.25, auto_reconnect=True,
        reconnect_max_delay_seconds=0.25, reconnect_request_timeout_seconds=5.0,
    )
    await first.start()
    assert first.node_ids is not None
    node_id = first.node_ids.production_count
    await client.connect()
    try:
        task = asyncio.create_task(client.collect_changes(
            [node_id], count=3, timeout_seconds=10.0,
            publishing_interval_ms=50.0, sampling_interval_ms=20.0,
        ))
        await asyncio.sleep(0.40)
        if task.done():
            raise AssertionError("subscription completed before outage")
        await first.stop()
        deadline = asyncio.get_running_loop().time() + 3.0
        while client.connection_state == "CONNECTED" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if client.connection_state not in {"DISCONNECTED", "RECONNECTING"}:
            raise AssertionError(f"outage state not observed: {client.connection_state}")
        replacement = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=0.05)
        await replacement.start()
        assert replacement.node_ids is not None
        if replacement.node_ids.production_count != node_id:
            raise AssertionError("replacement changed deterministic NodeId")
        await client.wait_until_connected(timeout_seconds=5.0)
        changes = await asyncio.wait_for(task, timeout=5.0)
        if len(changes) != 3 or any(item.quality is not Quality.GOOD for item in changes):
            raise AssertionError("active subscription was not restored")
        fresh = await client.read(node_id)
        if fresh.quality is not Quality.GOOD or fresh.trust is not TrustState.CURRENT:
            raise AssertionError("post-reconnect read is not GOOD/CURRENT")
        return "Existing subscription recovered after restart and fresh read is GOOD/CURRENT"
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.disconnect()
        if replacement is not None:
            await replacement.stop()
        await first.stop()


def _create_certificate(key_path: Path, cert_path: Path, uri: str, *, server: bool) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _now()
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Qualification"),
        x509.NameAttribute(NameOID.COMMON_NAME, "DevAgent Secure Simulator" if server else "DevAgent Secure Client"),
    ])
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(uri), x509.DNSName(socket.gethostname()),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))


def _materials(ctx: _Context) -> _SecureMaterials:
    if ctx.secure is not None:
        return ctx.secure
    server_key, server_cert = ctx.workdir / "server.pem", ctx.workdir / "server.der"
    wrong_key, wrong_cert = ctx.workdir / "wrong.pem", ctx.workdir / "wrong.der"
    client_key, client_cert = ctx.workdir / "client.pem", ctx.workdir / "client.der"
    client_uri = "urn:devagent:qualification:secure-client"
    _create_certificate(server_key, server_cert, OpcUaSimulator.APPLICATION_URI, server=True)
    _create_certificate(wrong_key, wrong_cert, "urn:devagent:qualification:wrong-server", server=True)
    _create_certificate(client_key, client_cert, client_uri, server=False)
    ctx.secure = _SecureMaterials(server_key, server_cert, wrong_cert, client_key, client_cert, client_uri)
    return ctx.secure


def _secure_client(endpoint: str, m: _SecureMaterials, *, password: str = _PASSWORD, pin: Path | None = None) -> ReadOnlyOpcUaClient:
    return ReadOnlyOpcUaClient(endpoint, auto_reconnect=False, security=LiveSecurityConfig(
        username="operator", password=password,
        security_policy="Basic256Sha256", security_mode="SignAndEncrypt",
        client_certificate=str(m.client_cert), client_private_key=str(m.client_key),
        server_certificate=str(pin or m.server_cert), application_uri=m.client_uri,
    ))


async def _secure_server(ctx: _Context, action: Callable[[str, OpcUaSimulator, _SecureMaterials], Awaitable[str]]) -> str:
    m = _materials(ctx)
    async with OpcUaSimulator(
        _free_endpoint("devagent/secure-qualification/"), scenario="blocker",
        username="operator", password=_PASSWORD,
        server_certificate=str(m.server_cert), server_private_key=str(m.server_key),
        security_policy="Basic256Sha256", security_mode="SignAndEncrypt",
    ) as simulator:
        return await action(simulator.endpoint, simulator, m)


async def _secure_good(ctx: _Context) -> str:
    async def action(endpoint: str, simulator: OpcUaSimulator, m: _SecureMaterials) -> str:
        assert simulator.node_ids is not None
        client = _secure_client(endpoint, m)
        await client.connect()
        try:
            value = await client.read(simulator.node_ids.machine_state)
            if value.value != "BLOCKED" or value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT:
                raise AssertionError("secure authenticated read did not return expected value")
            if client.authentication_mode != "USERNAME_PASSWORD" or client.security_summary != "Basic256Sha256/SignAndEncrypt":
                raise AssertionError("secure session metadata mismatch")
            return "Pinned SignAndEncrypt username/password session reads GOOD/CURRENT data"
        finally:
            await client.disconnect()
    return await _secure_server(ctx, action)


async def _wrong_password(ctx: _Context) -> str:
    async def action(endpoint: str, _sim: OpcUaSimulator, m: _SecureMaterials) -> str:
        client = _secure_client(endpoint, m, password=_WRONG_PASSWORD)
        try:
            await client.connect()
        except LiveConnectionError as exc:
            if _WRONG_PASSWORD in str(exc) or exc.__cause__ is not None:
                raise AssertionError("authentication error exposed secret or raw cause")
            return "Wrong password rejected without exposing the credential"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("wrong password unexpectedly authenticated")
    return await _secure_server(ctx, action)


async def _wrong_pin(ctx: _Context) -> str:
    async def action(endpoint: str, _sim: OpcUaSimulator, m: _SecureMaterials) -> str:
        client = _secure_client(endpoint, m, pin=m.wrong_server_cert)
        try:
            await client.connect()
        except LiveConnectionError:
            return "Server certificate mismatch rejected"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("wrong server certificate pin unexpectedly connected")
    return await _secure_server(ctx, action)


async def _anonymous_rejected(ctx: _Context) -> str:
    async def action(endpoint: str, _sim: OpcUaSimulator, _m: _SecureMaterials) -> str:
        client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
        try:
            await client.connect()
        except LiveConnectionError:
            return "Authenticated secure server rejected anonymous client"
        finally:
            if client.connected:
                await client.disconnect()
        raise AssertionError("anonymous client unexpectedly authenticated")
    return await _secure_server(ctx, action)


async def _start_three() -> tuple[list[OpcUaSimulator], dict[str, str]]:
    sims = [OpcUaSimulator(_free_endpoint(f"devagent/qualification/{name}/"), scenario="normal", update_interval_seconds=0.05) for name in "abc"]
    try:
        for sim in sims:
            await sim.start()
        nodes: dict[str, str] = {}
        for name, sim in zip("abc", sims):
            assert sim.node_ids is not None
            nodes[name] = sim.node_ids.production_count
        return sims, nodes
    except Exception:
        for sim in reversed(sims):
            await sim.stop()
        raise


def _manager(sims: list[OpcUaSimulator]) -> MultiPlcConnectionManager:
    return MultiPlcConnectionManager([
        PlcConnectionSpec(name, sim.endpoint, auto_reconnect=False)
        for name, sim in zip("abc", sims)
    ])


async def _three_plc(_ctx: _Context) -> str:
    sims, nodes = await _start_three()
    manager = _manager(sims)
    try:
        statuses = await manager.connect_all()
        if any(not item.connected for item in statuses.values()):
            raise AssertionError("not all three PLC sessions connected")
        reads = await manager.read_many({name: [nodes[name]] for name in nodes})
        for result in reads.values():
            if not result.succeeded or len(result.values) != 1:
                raise AssertionError("healthy three-PLC read failed")
            value = result.values[0]
            if value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT:
                raise AssertionError("three-PLC read lost GOOD/CURRENT trust")
        return "Three independent PLC sessions returned GOOD/CURRENT values"
    finally:
        await manager.disconnect_all()
        for sim in reversed(sims):
            await sim.stop()


async def _isolation(_ctx: _Context) -> str:
    sims, nodes = await _start_three()
    manager = _manager(sims)
    try:
        if any(not item.connected for item in (await manager.connect_all()).values()):
            raise AssertionError("initial three-PLC connection failed")
        await sims[1].stop()
        reads = await manager.read_many({name: [nodes[name]] for name in nodes})
        if reads["b"].succeeded:
            raise AssertionError("outage PLC B unexpectedly read successfully")
        for name in ("a", "c"):
            result = reads[name]
            if not result.succeeded or len(result.values) != 1:
                raise AssertionError(f"healthy PLC {name.upper()} was disrupted")
            value = result.values[0]
            if value.quality is not Quality.GOOD or value.trust is not TrustState.CURRENT:
                raise AssertionError(f"healthy PLC {name.upper()} lost trust")
        return "PLC B outage isolated while PLC A/C remained GOOD/CURRENT"
    finally:
        await manager.disconnect_all()
        for sim in reversed(sims):
            await sim.stop()


async def _runtime_read_only(_ctx: _Context) -> str:
    async with OpcUaSimulator(_free_endpoint(), scenario="normal") as simulator:
        client = ReadOnlyOpcUaClient(simulator.endpoint, auto_reconnect=False)
        await client.connect()
        try:
            variables = [item for item in await client.browse(max_depth=4, max_nodes=100) if item.node_class == "Variable"]
            if not variables or any(item.writable or not item.readable for item in variables):
                raise AssertionError("runtime browse did not remain readable/non-writable")
            return f"Browsed {len(variables)} Variables; all readable and non-writable"
        finally:
            await client.disconnect()


_RUNNERS: dict[str, CaseRunner] = dict(zip(
    (item.case_id for item in LIVE_RELEASE_QUALIFICATION_CASES),
    (_read_only_surface, _trust_gate, _unreachable, _anonymous_typed, _bad_node, _subscription,
     _reconnect, _secure_good, _wrong_password, _wrong_pin, _anonymous_rejected,
     _three_plc, _isolation, _runtime_read_only),
))


async def run_live_release_qualification(
    *,
    runtime_available: bool | None = None,
    runtime_version: str | None = None,
    runner_overrides: Mapping[str, CaseRunner] | None = None,
) -> LiveReleaseQualificationReport:
    detected_available, detected_version = _runtime_info()
    available = detected_available if runtime_available is None else bool(runtime_available)
    version = detected_version if runtime_version is None else runtime_version
    runners = dict(_RUNNERS)
    if runner_overrides:
        unknown = sorted(set(runner_overrides) - set(runners))
        if unknown:
            raise ValueError("Unknown qualification case override(s): " + ", ".join(unknown))
        runners.update(runner_overrides)

    started = _now()
    results: list[LiveQualificationCaseResult] = []
    with TemporaryDirectory(prefix="devagent-live-qualification-") as temp:
        ctx = _Context(Path(temp))
        for case in LIVE_RELEASE_QUALIFICATION_CASES:
            t0 = time.monotonic()
            blocked: str | None = None
            if case.runtime_required and not available:
                blocked = 'asyncua runtime unavailable; install with: python -m pip install "devagent-ai[live]"'
            elif case.runtime_required and version is not None and not version.startswith("2."):
                blocked = f"unsupported asyncua runtime {version}; DevAgent Live requires asyncua>=2.0,<3"
            if blocked is not None:
                results.append(LiveQualificationCaseResult(
                    case.case_id, case.title, LiveQualificationStatus.BLOCKED,
                    blocked, True, time.monotonic() - t0,
                ))
                continue
            try:
                detail = await runners[case.case_id](ctx)
            except LiveDependencyError as exc:
                status, detail = LiveQualificationStatus.BLOCKED, str(exc)
            except Exception as exc:
                status, detail = LiveQualificationStatus.FAIL, f"{type(exc).__name__}: {exc}"
            else:
                status, detail = LiveQualificationStatus.PASS, detail or "Qualification case passed"
            results.append(LiveQualificationCaseResult(
                case.case_id, case.title, status, _redact(detail),
                case.runtime_required, time.monotonic() - t0,
            ))
    return LiveReleaseQualificationReport(
        started, _now(), available, version if available else None, tuple(results)
    )


def write_live_release_qualification_artifacts(output_dir: Path, report: LiveReleaseQualificationReport) -> Path:
    destination = Path(output_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=False)
    report_path = destination / "live_release_qualification.json"
    manifest_path = destination / "manifest.json"
    try:
        payload = json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        report_path.write_text(payload, encoding="utf-8")
        encoded = payload.encode("utf-8")
        manifest = {
            "schema": "devagent-live-release-qualification-manifest-v1",
            "mode": "READ_ONLY",
            "qualification_status": report.status.value,
            "artifacts": {
                report_path.name: {
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                }
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        for artifact in (manifest_path, report_path):
            try:
                artifact.unlink()
            except FileNotFoundError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
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
