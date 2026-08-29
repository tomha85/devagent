from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from .errors import LiveConnectionError
from .models import RuntimeValue
from .opcua_client import ReadOnlyOpcUaClient
from .security import LiveSecurityConfig, validate_opcua_endpoint


class PlcSessionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


@dataclass(frozen=True)
class PlcConnectionSpec:
    plc_id: str
    endpoint: str
    plc_name: str | None = None
    security: LiveSecurityConfig = field(default_factory=LiveSecurityConfig, repr=False)
    timeout_seconds: float = 4.0
    auto_reconnect: bool = True
    reconnect_max_delay_seconds: float = 5.0
    reconnect_request_timeout_seconds: float = 30.0
    stale_after_seconds: float = 5.0

    def __post_init__(self) -> None:
        plc_id = self.plc_id.strip()
        if not plc_id:
            raise ValueError("plc_id cannot be blank")
        object.__setattr__(self, "plc_id", plc_id)
        object.__setattr__(self, "endpoint", validate_opcua_endpoint(self.endpoint))
        if self.plc_name is not None:
            name = self.plc_name.strip()
            if not name:
                raise ValueError("plc_name cannot be blank when provided")
            object.__setattr__(self, "plc_name", name)
        for label, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("reconnect_max_delay_seconds", self.reconnect_max_delay_seconds),
            ("reconnect_request_timeout_seconds", self.reconnect_request_timeout_seconds),
            ("stale_after_seconds", self.stale_after_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be > 0")

    @property
    def display_name(self) -> str:
        return self.plc_name or self.plc_id


@dataclass(frozen=True)
class ManagedPlcStatus:
    plc_id: str
    plc_name: str
    endpoint: str
    state: PlcSessionState
    connected: bool
    authentication_mode: str
    security_summary: str
    successful_connections: int
    last_error: str | None
    changed_at: datetime


@dataclass(frozen=True)
class PlcReadResult:
    plc_id: str
    values: tuple[RuntimeValue, ...]
    state: PlcSessionState
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass
class _ManagedPlc:
    spec: PlcConnectionSpec
    state: PlcSessionState = PlcSessionState.DISCONNECTED
    client: Any | None = None
    successful_connections: int = 0
    last_error: str | None = None
    changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


ClientFactory = Callable[..., Any]


class MultiPlcConnectionManager:
    """Own multiple independent read-only OPC UA sessions.

    Each PLC has its own client and lock. Bulk operations isolate per-PLC
    failures so one unavailable controller does not cancel healthy sessions.
    """

    def __init__(
        self,
        specs: Iterable[PlcConnectionSpec],
        *,
        client_factory: ClientFactory = ReadOnlyOpcUaClient,
    ) -> None:
        self._client_factory = client_factory
        self._plcs: dict[str, _ManagedPlc] = {}
        endpoints: set[str] = set()
        for spec in specs:
            if spec.plc_id in self._plcs:
                raise ValueError(f"Duplicate PLC id: {spec.plc_id}")
            if spec.endpoint in endpoints:
                raise ValueError(f"Duplicate OPC UA endpoint: {spec.endpoint}")
            endpoints.add(spec.endpoint)
            self._plcs[spec.plc_id] = _ManagedPlc(spec=spec)
        if not self._plcs:
            raise ValueError("At least one PLC connection spec is required")

    @property
    def plc_ids(self) -> tuple[str, ...]:
        return tuple(self._plcs)

    def _entry(self, plc_id: str) -> _ManagedPlc:
        try:
            return self._plcs[plc_id]
        except KeyError as exc:
            raise KeyError(f"Unknown PLC id: {plc_id}") from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _safe_error(self, entry: _ManagedPlc, exc: BaseException) -> str:
        return entry.spec.security.redact(str(exc))

    def _set_state(
        self,
        entry: _ManagedPlc,
        state: PlcSessionState,
        *,
        error: str | None = None,
    ) -> None:
        entry.state = state
        entry.last_error = error
        entry.changed_at = self._now()

    def _build_client(self, spec: PlcConnectionSpec) -> Any:
        return self._client_factory(
            spec.endpoint,
            timeout_seconds=spec.timeout_seconds,
            auto_reconnect=spec.auto_reconnect,
            reconnect_max_delay_seconds=spec.reconnect_max_delay_seconds,
            reconnect_request_timeout_seconds=spec.reconnect_request_timeout_seconds,
            stale_after_seconds=spec.stale_after_seconds,
            security=spec.security,
        )

    def status(self, plc_id: str) -> ManagedPlcStatus:
        entry = self._entry(plc_id)
        state = entry.state
        connected = False
        client = entry.client
        if client is not None:
            raw_state = str(getattr(client, "connection_state", "UNKNOWN")).strip().upper()
            connected = bool(getattr(client, "connected", False))
            if state not in {PlcSessionState.CONNECTING, PlcSessionState.STOPPING, PlcSessionState.FAILED}:
                if raw_state in {"CONNECTING", "RECONNECTING"}:
                    state = PlcSessionState.RECONNECTING
                elif raw_state in {"DISCONNECTED", "DISCONNECTING"} and state is PlcSessionState.CONNECTED:
                    state = PlcSessionState.DEGRADED
                elif connected or raw_state == "CONNECTED":
                    connected = True
                    if state is not PlcSessionState.DEGRADED:
                        state = PlcSessionState.CONNECTED

        return ManagedPlcStatus(
            plc_id=entry.spec.plc_id,
            plc_name=entry.spec.display_name,
            endpoint=entry.spec.endpoint,
            state=state,
            connected=connected,
            authentication_mode=entry.spec.security.authentication_mode,
            security_summary=entry.spec.security.channel_summary,
            successful_connections=entry.successful_connections,
            last_error=entry.last_error,
            changed_at=entry.changed_at,
        )

    def statuses(self) -> dict[str, ManagedPlcStatus]:
        return {plc_id: self.status(plc_id) for plc_id in self._plcs}

    async def connect(self, plc_id: str) -> ManagedPlcStatus:
        entry = self._entry(plc_id)
        async with entry.lock:
            if entry.client is not None and bool(getattr(entry.client, "connected", False)):
                self._set_state(entry, PlcSessionState.CONNECTED)
                return self.status(plc_id)

            old_client, entry.client = entry.client, None
            if old_client is not None:
                try:
                    await old_client.disconnect()
                except Exception:
                    pass

            self._set_state(entry, PlcSessionState.CONNECTING)
            client: Any | None = None
            try:
                client = self._build_client(entry.spec)
                entry.client = client
                await client.connect()
                if not bool(getattr(client, "connected", False)):
                    raise LiveConnectionError(
                        f"initial session did not reach CONNECTED; state={getattr(client, 'connection_state', 'UNKNOWN')}"
                    )
            except Exception as exc:
                safe = self._safe_error(entry, exc)
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                entry.client = None
                self._set_state(entry, PlcSessionState.FAILED, error=safe)
                raise LiveConnectionError(f"PLC {plc_id} connect failed: {safe}") from None

            entry.successful_connections += 1
            self._set_state(entry, PlcSessionState.CONNECTED)
            return self.status(plc_id)

    async def _connect_isolated(self, plc_id: str) -> ManagedPlcStatus:
        try:
            return await self.connect(plc_id)
        except Exception:
            return self.status(plc_id)

    async def connect_all(self) -> dict[str, ManagedPlcStatus]:
        tasks = [asyncio.create_task(self._connect_isolated(plc_id)) for plc_id in self._plcs]
        results = await asyncio.gather(*tasks)
        return {status.plc_id: status for status in results}

    async def disconnect(self, plc_id: str) -> ManagedPlcStatus:
        entry = self._entry(plc_id)
        async with entry.lock:
            client, entry.client = entry.client, None
            if client is None:
                self._set_state(entry, PlcSessionState.DISCONNECTED)
                return self.status(plc_id)

            self._set_state(entry, PlcSessionState.STOPPING)
            try:
                await client.disconnect()
            except Exception as exc:
                safe = self._safe_error(entry, exc)
                self._set_state(entry, PlcSessionState.FAILED, error=safe)
                raise LiveConnectionError(f"PLC {plc_id} disconnect failed: {safe}") from None

            self._set_state(entry, PlcSessionState.DISCONNECTED)
            return self.status(plc_id)

    async def _disconnect_isolated(self, plc_id: str) -> ManagedPlcStatus:
        try:
            return await self.disconnect(plc_id)
        except Exception:
            return self.status(plc_id)

    async def disconnect_all(self) -> dict[str, ManagedPlcStatus]:
        tasks = [asyncio.create_task(self._disconnect_isolated(plc_id)) for plc_id in self._plcs]
        results = await asyncio.gather(*tasks)
        return {status.plc_id: status for status in results}

    async def read(self, plc_id: str, node_id: str) -> RuntimeValue:
        entry = self._entry(plc_id)
        async with entry.lock:
            client = entry.client
            if client is None or not bool(getattr(client, "connected", False)):
                observed = self.status(plc_id)
                raise LiveConnectionError(
                    f"PLC {plc_id} session is not connected; state={observed.state.value}"
                )

            try:
                value = await client.read(node_id)
            except Exception as exc:
                safe = self._safe_error(entry, exc)
                raw_state = str(getattr(client, "connection_state", "UNKNOWN")).strip().upper()
                next_state = (
                    PlcSessionState.RECONNECTING
                    if raw_state in {"CONNECTING", "RECONNECTING"}
                    else PlcSessionState.DEGRADED
                )
                self._set_state(entry, next_state, error=safe)
                raise LiveConnectionError(
                    f"PLC {plc_id} read failed for {node_id}: {safe}"
                ) from None

            self._set_state(entry, PlcSessionState.CONNECTED)
            return value

    async def _read_batch_isolated(
        self,
        plc_id: str,
        node_ids: Iterable[str],
    ) -> PlcReadResult:
        values: list[RuntimeValue] = []
        try:
            for node_id in node_ids:
                values.append(await self.read(plc_id, node_id))
            return PlcReadResult(
                plc_id=plc_id,
                values=tuple(values),
                state=self.status(plc_id).state,
            )
        except Exception as exc:
            entry = self._entry(plc_id)
            safe = self._safe_error(entry, exc)
            return PlcReadResult(
                plc_id=plc_id,
                values=tuple(values),
                state=self.status(plc_id).state,
                error=safe,
            )

    async def read_many(
        self,
        node_ids_by_plc: Mapping[str, Iterable[str]],
    ) -> dict[str, PlcReadResult]:
        unknown = [plc_id for plc_id in node_ids_by_plc if plc_id not in self._plcs]
        if unknown:
            raise KeyError(f"Unknown PLC id(s): {', '.join(unknown)}")
        tasks = [
            asyncio.create_task(self._read_batch_isolated(plc_id, tuple(node_ids)))
            for plc_id, node_ids in node_ids_by_plc.items()
        ]
        results = await asyncio.gather(*tasks)
        return {result.plc_id: result for result in results}

    async def __aenter__(self) -> "MultiPlcConnectionManager":
        await self.connect_all()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.disconnect_all()
