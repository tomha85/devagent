from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import ssl
from typing import Any, Iterable
from urllib.parse import urlsplit

from cryptography import x509

from .errors import LiveConnectionError, LiveDependencyError
from .models import BrowseNode, EndpointSummary, Quality, RuntimeValue
from .security import LiveSecurityConfig, validate_opcua_endpoint


def _require_asyncua() -> tuple[Any, Any]:
    try:
        from asyncua import Client, ua
    except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
        raise LiveDependencyError(
            'DevAgent Live requires the optional OPC UA runtime. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc
    return Client, ua


def _require_security_policies() -> Any:
    try:
        from asyncua.crypto import security_policies
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise LiveDependencyError(
            'DevAgent Live secure OPC UA requires the optional OPC UA runtime. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc
    return security_policies


def _require_trust_runtime() -> tuple[Any, Any, Any]:
    try:
        from asyncua.crypto.truststore import TrustStore
        from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise LiveDependencyError(
            'DevAgent Live OPC UA trust-store validation requires the optional OPC UA runtime. '
            'Install it with: python -m pip install "devagent-ai[live]"'
        ) from exc
    return TrustStore, CertificateValidator, CertificateValidatorOptions


def _validate_server_certificate_hostname(certificate: x509.Certificate, endpoint: str) -> None:
    """Require the OPC UA server certificate SAN to match the configured endpoint host.

    asyncua 2.0.x validates trust, lifetime, ApplicationURI, key usage, and
    revocation, but its hostname check is not active. DevAgent adds this check
    for trust-store/CA mode so a different server certificate issued by the
    same trusted CA cannot impersonate the configured PLC endpoint.
    """

    host = urlsplit(endpoint).hostname
    if not host:
        raise LiveConnectionError("OPC UA endpoint host is unavailable for certificate validation")
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as exc:
        raise LiveConnectionError(
            f"OPC UA server certificate has no SubjectAlternativeName for endpoint host {host!r}"
        ) from exc

    presented: list[tuple[str, str]] = []
    presented.extend(("DNS", value) for value in san.get_values_for_type(x509.DNSName))
    presented.extend(
        ("IP Address", str(value)) for value in san.get_values_for_type(x509.IPAddress)
    )
    if not presented:
        raise LiveConnectionError(
            f"OPC UA server certificate has no DNS/IP SubjectAlternativeName for endpoint host {host!r}"
        )
    try:
        ssl.match_hostname({"subjectAltName": presented}, host)
    except ssl.CertificateError as exc:
        raise LiveConnectionError(
            f"OPC UA server certificate does not match endpoint host {host!r}"
        ) from exc


def _node_id_text(node_id: Any) -> str:
    to_string = getattr(node_id, "to_string", None)
    if callable(to_string):
        return str(to_string())
    return str(node_id)


def _status_name(status: Any) -> str:
    name = getattr(status, "name", None)
    if name:
        return str(name)
    return str(status)


def _is_graceful_shutdown_status(status: Any) -> bool:
    # A server can publish BadShutdown before the transport-loss callback has
    # moved the client state away from CONNECTED. Other bad statuses remain
    # fail-closed unless the public connection state already shows recovery.
    return _status_name(status) == "BadShutdown"


def _quality_from_status(status: Any) -> Quality:
    if status is None:
        return Quality.BAD
    try:
        if status.is_good():
            return Quality.GOOD
        if status.is_bad():
            return Quality.BAD
    except Exception:
        return Quality.BAD
    return Quality.UNCERTAIN


def _utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _runtime_value_from_datavalue(
    node_id: str,
    data_value: Any,
    *,
    stale_after_seconds: float,
    replayed: bool = False,
) -> RuntimeValue:
    received_at = datetime.now(timezone.utc)
    status = getattr(data_value, "StatusCode", None)
    quality = _quality_from_status(status)
    source_timestamp = _utc_timestamp(getattr(data_value, "SourceTimestamp", None))
    server_timestamp = _utc_timestamp(getattr(data_value, "ServerTimestamp", None))
    freshness_timestamp = source_timestamp or server_timestamp or received_at
    age_seconds = max(0.0, (received_at - freshness_timestamp).total_seconds())
    stale = age_seconds > stale_after_seconds

    variant = getattr(data_value, "Value", None)
    value = getattr(variant, "Value", None) if variant is not None else None
    variant_type = getattr(variant, "VariantType", None) if variant is not None else None
    variant_name = getattr(variant_type, "name", None) if variant_type is not None else None

    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type=str(variant_name) if variant_name else (str(variant_type) if variant_type else None),
        status_code=_status_name(status),
        quality=quality,
        source_timestamp=source_timestamp,
        server_timestamp=server_timestamp,
        received_at=received_at,
        age_seconds=age_seconds,
        stale=stale,
        replayed=replayed,
    )


class ReadOnlyOpcUaClient:
    """Small OPC UA client surface for DevAgent Live.

    The public API intentionally exposes discovery, browse, read, subscribe, and
    disconnect only. There is no write, set-value, force, or method-call API.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 4.0,
        auto_reconnect: bool = True,
        reconnect_max_delay_seconds: float = 5.0,
        reconnect_request_timeout_seconds: float = 30.0,
        stale_after_seconds: float = 5.0,
        security: LiveSecurityConfig | None = None,
    ) -> None:
        self.endpoint = validate_opcua_endpoint(endpoint)
        self.timeout_seconds = timeout_seconds
        self.auto_reconnect = auto_reconnect
        self.reconnect_max_delay_seconds = reconnect_max_delay_seconds
        self.reconnect_request_timeout_seconds = reconnect_request_timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.security = security or LiveSecurityConfig()
        self._client: Any | None = None

    @property
    def connection_state(self) -> str:
        """Return the underlying asyncua connection state without exposing its enum type."""

        if self._client is None:
            return "DISCONNECTED"
        state = getattr(self._client, "state", None)
        value = getattr(state, "value", state)
        if value is None:
            return "UNKNOWN"
        return str(value).strip().upper()

    @property
    def connected(self) -> bool:
        return self.connection_state == "CONNECTED"

    @property
    def authentication_mode(self) -> str:
        return self.security.authentication_mode

    @property
    def security_summary(self) -> str:
        return self.security.channel_summary

    async def wait_until_connected(self, *, timeout_seconds: float = 30.0) -> None:
        """Wait for asyncua's reconnect supervisor to restore the session.

        This method does not perform a second reconnect loop. It only observes
        asyncua's public state-subscription API so DevAgent has one reconnect
        authority and does not race the library's session/subscription recovery.
        """

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        client = self._require_connected()
        if self.connected:
            return

        subscribe_state = getattr(client, "subscribe_state", None)
        if not callable(subscribe_state):
            raise LiveConnectionError("Installed asyncua does not expose reconnect state notifications")

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            async with subscribe_state() as states:
                while True:
                    state = self.connection_state
                    if state == "CONNECTED":
                        return
                    if state in {"DISCONNECTED", "DISCONNECTING"} and not self.auto_reconnect:
                        raise LiveConnectionError(f"OPC UA session is {state.lower()}")

                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise LiveConnectionError(
                            f"Timed out waiting for OPC UA reconnect; state={self.connection_state}"
                        )
                    try:
                        await states.next_change(timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise LiveConnectionError(
                            f"Timed out waiting for OPC UA reconnect; state={self.connection_state}"
                        ) from exc
        except LiveConnectionError:
            raise
        except Exception as exc:
            raise LiveConnectionError(f"Unable to observe OPC UA reconnect state: {exc}") from exc

    async def discover_endpoints(self) -> list[EndpointSummary]:
        Client, _ua = _require_asyncua()
        client = Client(url=self.endpoint, timeout=self.timeout_seconds)
        try:
            endpoints = await client.connect_and_get_server_endpoints()
        except Exception as exc:
            message = self.security.redact(str(exc))
            raise LiveConnectionError(
                f"Unable to discover OPC UA endpoint {self.endpoint}: {message}"
            ) from None

        summaries: list[EndpointSummary] = []
        for endpoint in endpoints:
            token_types: list[str] = []
            for token in getattr(endpoint, "UserIdentityTokens", []) or []:
                token_type = getattr(token, "TokenType", None)
                token_name = getattr(token_type, "name", None)
                token_types.append(str(token_name or token_type))
            server = getattr(endpoint, "Server", None)
            app_name = getattr(server, "ApplicationName", None)
            to_string = getattr(app_name, "to_string", None)
            if callable(to_string):
                app_text = str(to_string())
            else:
                app_text = str(getattr(app_name, "Text", app_name) or "")
            summaries.append(
                EndpointSummary(
                    endpoint_url=str(getattr(endpoint, "EndpointUrl", self.endpoint)),
                    security_mode=str(
                        getattr(getattr(endpoint, "SecurityMode", None), "name", None)
                        or getattr(endpoint, "SecurityMode", "")
                    ),
                    security_policy_uri=str(getattr(endpoint, "SecurityPolicyUri", "")),
                    user_token_types=tuple(token_types),
                    server_application_name=app_text,
                )
            )
        return summaries

    async def _configure_client_security(self, client: Any, ua: Any) -> None:
        self.security.validate_files()

        if self.security.username is not None:
            client.set_user(self.security.username)
            assert self.security.password is not None
            client.set_password(self.security.password)

        if self.security.secure_channel:
            security_policies = _require_security_policies()
            policy = getattr(
                security_policies,
                f"SecurityPolicy{self.security.security_policy}",
                None,
            )
            if policy is None:
                raise LiveConnectionError(
                    f"Installed asyncua does not support security policy {self.security.security_policy}"
                )
            mode = getattr(ua.MessageSecurityMode, str(self.security.security_mode), None)
            if mode is None:
                raise LiveConnectionError(
                    f"Installed asyncua does not support security mode {self.security.security_mode}"
                )

            if self.security.trust_store is not None:
                TrustStore, CertificateValidator, CertificateValidatorOptions = _require_trust_runtime()
                trust_store = TrustStore(
                    [Path(self.security.trust_store)],
                    [Path(self.security.crl_store)] if self.security.crl_store is not None else [],
                )
                await trust_store.load()
                base_validator = CertificateValidator(
                    CertificateValidatorOptions.TRUSTED_VALIDATION
                    | CertificateValidatorOptions.PEER_SERVER,
                    trust_store,
                )

                async def validate_server_certificate(certificate: x509.Certificate, app_description: Any) -> None:
                    await base_validator(certificate, app_description)
                    _validate_server_certificate_hostname(certificate, self.endpoint)

                client.certificate_validator = validate_server_certificate

            client.application_uri = self.security.application_uri
            await client.set_security(
                policy,
                certificate=str(self.security.client_certificate),
                private_key=str(self.security.client_private_key),
                private_key_password=self.security.private_key_password,
                server_certificate=(
                    str(self.security.server_certificate)
                    if self.security.server_certificate is not None
                    else None
                ),
                mode=mode,
            )

        if self.security.user_certificate is not None:
            load_user_certificate = getattr(client, "load_client_certificate", None)
            load_user_private_key = getattr(client, "load_private_key", None)
            if not callable(load_user_certificate) or not callable(load_user_private_key):
                raise LiveConnectionError(
                    "Installed asyncua does not expose X.509 user-certificate authentication loaders"
                )
            assert self.security.user_private_key is not None
            await load_user_certificate(str(self.security.user_certificate))
            await load_user_private_key(
                str(self.security.user_private_key),
                password=self.security.user_private_key_password,
            )

    async def connect(self) -> None:
        if self._client is not None:
            return
        Client, ua = _require_asyncua()
        # asyncua 2.0 exposes reconnect controls on connect(); 2.0.1 also
        # accepts them in the constructor. Keep construction compatible with
        # both supported releases and configure reconnect in one place.
        client = Client(url=self.endpoint, timeout=self.timeout_seconds)
        try:
            await self._configure_client_security(client, ua)
            await client.connect(
                auto_reconnect=self.auto_reconnect,
                reconnect_max_delay=self.reconnect_max_delay_seconds,
                reconnect_request_timeout=self.reconnect_request_timeout_seconds,
            )
        except LiveDependencyError:
            raise
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            message = self.security.redact(str(exc))
            raise LiveConnectionError(
                f"Unable to connect OPC UA session {self.endpoint}: {message}"
            ) from None
        self._client = client

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as exc:
            message = self.security.redact(str(exc))
            raise LiveConnectionError(
                f"Error while closing OPC UA session {self.endpoint}: {message}"
            ) from None

    def _require_connected(self) -> Any:
        if self._client is None:
            raise LiveConnectionError("OPC UA session is not connected")
        return self._client

    async def browse(self, *, max_depth: int = 4, max_nodes: int = 500) -> list[BrowseNode]:
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")

        client = self._require_connected()
        _Client, ua = _require_asyncua()
        root = client.nodes.objects
        queue: deque[tuple[Any, str, int]] = deque([(root, "Objects", 0)])
        results: list[BrowseNode] = []
        visited: set[str] = set()

        while queue and len(results) < max_nodes:
            parent, parent_path, depth = queue.popleft()
            if depth >= max_depth:
                continue
            try:
                children = await parent.get_children()
            except Exception as exc:
                raise LiveConnectionError(f"Browse failed below {parent_path}: {exc}") from exc

            for child in children:
                node_id = _node_id_text(child.nodeid)
                if node_id in visited:
                    continue
                visited.add(node_id)
                try:
                    browse_name_obj = await child.read_browse_name()
                    browse_name = str(getattr(browse_name_obj, "Name", browse_name_obj))
                    display_name_obj = await child.read_display_name()
                    display_name = str(getattr(display_name_obj, "Text", display_name_obj))
                    node_class_obj = await child.read_node_class()
                    node_class = str(getattr(node_class_obj, "name", node_class_obj))
                except Exception:
                    continue

                path = f"{parent_path}.{browse_name}" if parent_path else browse_name
                data_type: str | None = None
                access_names: tuple[str, ...] = ()
                readable = False
                writable = False

                if node_class_obj == ua.NodeClass.Variable:
                    try:
                        variant_type = await child.read_data_type_as_variant_type()
                        data_type = str(getattr(variant_type, "name", variant_type))
                    except Exception:
                        data_type = None
                    try:
                        access = await child.get_user_access_level()
                        names = sorted(str(getattr(item, "name", item)) for item in access)
                        access_names = tuple(names)
                        readable = "CurrentRead" in names
                        writable = "CurrentWrite" in names
                    except Exception:
                        access_names = ()
                        readable = False
                        writable = False

                results.append(
                    BrowseNode(
                        path=path,
                        node_id=node_id,
                        browse_name=browse_name,
                        display_name=display_name,
                        node_class=node_class,
                        data_type=data_type,
                        user_access=access_names,
                        readable=readable,
                        writable=writable,
                    )
                )
                if len(results) >= max_nodes:
                    break
                if depth + 1 < max_depth:
                    queue.append((child, path, depth + 1))

        return results

    async def read(self, node_id: str) -> RuntimeValue:
        client = self._require_connected()
        node = client.get_node(node_id)
        try:
            data_value = await node.read_data_value(raise_on_bad_status=False)
        except Exception as exc:
            raise LiveConnectionError(f"Read failed for {node_id}: {exc}") from exc
        return _runtime_value_from_datavalue(
            node_id,
            data_value,
            stale_after_seconds=self.stale_after_seconds,
        )

    async def load_values(
        self,
        nodes: Iterable[BrowseNode],
        *,
        max_values: int = 200,
    ) -> list[RuntimeValue]:
        values: list[RuntimeValue] = []
        for node in nodes:
            if len(values) >= max_values:
                break
            if node.node_class != "Variable" or not node.readable:
                continue
            values.append(await self.read(node.node_id))
        return values

    async def collect_changes(
        self,
        node_ids: Iterable[str],
        *,
        count: int = 2,
        timeout_seconds: float = 5.0,
        publishing_interval_ms: float = 250.0,
        sampling_interval_ms: float = 100.0,
        queue_size: int = 10,
    ) -> list[RuntimeValue]:
        if count < 1:
            raise ValueError("count must be >= 1")
        client = self._require_connected()
        _Client, _ua = _require_asyncua()
        from asyncua.common.subscription import DataChangeEvent, StatusChangeEvent

        nodes = [client.get_node(node_id) for node_id in node_ids]
        if not nodes:
            return []

        values: list[RuntimeValue] = []

        async with await client.create_subscription(publishing_interval_ms) as subscription:
            await subscription.subscribe_data_change(
                nodes,
                queuesize=queue_size,
                sampling_interval=sampling_interval_ms,
            )
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while len(values) < count:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    event = await subscription.next_event(timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if isinstance(event, StatusChangeEvent):
                    status = event.notification.Status
                    if status is not None and status.is_bad():
                        reconnecting = self.auto_reconnect and (
                            _is_graceful_shutdown_status(status)
                            or self.connection_state in {
                                "CONNECTING",
                                "DISCONNECTED",
                                "RECONNECTING",
                            }
                        )
                        if not reconnecting:
                            raise LiveConnectionError(
                                f"Subscription status changed to {_status_name(status)}"
                            )
                    continue
                if not isinstance(event, DataChangeEvent):
                    continue
                data_value = event.data.monitored_item.Value
                values.append(
                    _runtime_value_from_datavalue(
                        _node_id_text(event.node.nodeid),
                        data_value,
                        stale_after_seconds=self.stale_after_seconds,
                        replayed=event.replayed,
                    )
                )
        return values
