from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devagent.live.errors import LiveConnectionError
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig
import devagent.live.opcua_client as opcua_client


class _Mode:
    SignAndEncrypt = object()


class _Policies:
    SecurityPolicyBasic256Sha256 = object()


class _FailingClient:
    def __init__(self, **kwargs) -> None:
        self.state = SimpleNamespace(value="disconnected")

    def set_user(self, value: str) -> None:
        return None

    def set_password(self, value: str) -> None:
        return None

    async def set_security(self, *args, **kwargs) -> None:
        return None

    async def connect(self, **kwargs) -> None:
        raise RuntimeError("authentication failed for super-secret")

    async def disconnect(self) -> None:
        return None


def test_authentication_error_does_not_chain_raw_secret(monkeypatch, tmp_path) -> None:
    certificate = tmp_path / "client.der"
    private_key = tmp_path / "client.pem"
    server_certificate = tmp_path / "server.der"
    for path in (certificate, private_key, server_certificate):
        path.write_text("test", encoding="utf-8")

    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (_FailingClient, SimpleNamespace(MessageSecurityMode=_Mode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)

    security = LiveSecurityConfig(
        username="operator",
        password="super-secret",
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=str(certificate),
        client_private_key=str(private_key),
        server_certificate=str(server_certificate),
    )

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient(
            "opc.tcp://127.0.0.1:4840/",
            security=security,
            auto_reconnect=False,
        )
        with pytest.raises(LiveConnectionError) as info:
            await client.connect()
        error = info.value
        assert "super-secret" not in str(error)
        assert error.__cause__ is None
        assert error.__suppress_context__ is True

    asyncio.run(scenario())
