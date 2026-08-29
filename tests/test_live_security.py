from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import pytest

from devagent.live.cli_security import add_security_args, security_from_args
from devagent.live.errors import LiveConfigurationError, LiveConnectionError
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig, validate_opcua_endpoint
import devagent.live.opcua_client as opcua_client


class _State:
    value = "connected"


class _FakeMode:
    Sign = object()
    SignAndEncrypt = object()


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.state = _State()
        self.application_uri = None
        self.user = None
        self.password = None
        self.security_call = None
        self.connect_call = None
        type(self).instances.append(self)

    def set_user(self, value: str) -> None:
        self.user = value

    def set_password(self, value: str) -> None:
        self.password = value

    async def set_security(self, *args, **kwargs) -> None:
        self.security_call = (args, kwargs)

    async def connect(self, **kwargs) -> None:
        self.connect_call = kwargs

    async def disconnect(self) -> None:
        return None


class _Policies:
    SecurityPolicyBasic256Sha256 = object()
    SecurityPolicyAes128Sha256RsaOaep = object()
    SecurityPolicyAes256Sha256RsaPss = object()


def _security_files(tmp_path) -> tuple[str, str, str]:
    paths = []
    for name in ("client.der", "client.pem", "server.der"):
        path = tmp_path / name
        path.write_text("test", encoding="utf-8")
        paths.append(str(path))
    return paths[0], paths[1], paths[2]


def _secure_config(tmp_path) -> LiveSecurityConfig:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    return LiveSecurityConfig(
        username="operator",
        password="plc-secret",
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=certificate,
        client_private_key=private_key,
        private_key_password="key-secret",
        server_certificate=server_certificate,
        application_uri="urn:devagent:test-client",
    )


def test_anonymous_unsecured_is_valid() -> None:
    config = LiveSecurityConfig()
    assert config.authentication_mode == "ANONYMOUS"
    assert config.channel_summary == "NONE"
    config.validate_files()


def test_endpoint_rejects_embedded_credentials() -> None:
    with pytest.raises(LiveConfigurationError, match="embedded"):
        validate_opcua_endpoint("opc.tcp://user:secret@127.0.0.1:4840/")


def test_username_password_requires_sign_and_encrypt() -> None:
    with pytest.raises(LiveConfigurationError, match="SignAndEncrypt"):
        LiveSecurityConfig(username="operator", password="pw")


def test_secure_config_requires_pinned_server_certificate(tmp_path) -> None:
    certificate = tmp_path / "client.der"
    certificate.write_text("test", encoding="utf-8")
    private_key = tmp_path / "client.pem"
    private_key.write_text("test", encoding="utf-8")
    with pytest.raises(LiveConfigurationError, match="pinned server certificate"):
        LiveSecurityConfig(
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=str(certificate),
            client_private_key=str(private_key),
        )


def test_secure_files_are_checked_and_repr_redacts_secrets(tmp_path) -> None:
    config = _secure_config(tmp_path)
    config.validate_files()
    rendered = repr(config)
    assert "plc-secret" not in rendered
    assert "key-secret" not in rendered
    assert config.redact("bad plc-secret and key-secret") == "bad <redacted> and <redacted>"


def test_sign_only_rejects_username_password(tmp_path) -> None:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    with pytest.raises(LiveConfigurationError, match="SignAndEncrypt"):
        LiveSecurityConfig(
            username="operator",
            password="pw",
            security_policy="Basic256Sha256",
            security_mode="Sign",
            client_certificate=certificate,
            client_private_key=private_key,
            server_certificate=server_certificate,
        )


def test_secure_connect_applies_authentication_and_pinned_security(monkeypatch, tmp_path) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (_FakeClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    config = _secure_config(tmp_path)

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        await client.connect()
        raw = _FakeClient.instances[-1]
        assert raw.user == "operator"
        assert raw.password == "plc-secret"
        assert raw.application_uri == "urn:devagent:test-client"
        args, kwargs = raw.security_call
        assert args[0] is _Policies.SecurityPolicyBasic256Sha256
        assert kwargs["server_certificate"].endswith("server.der")
        assert kwargs["private_key_password"] == "key-secret"
        assert kwargs["mode"] is _FakeMode.SignAndEncrypt
        assert client.authentication_mode == "USERNAME_PASSWORD"
        assert client.security_summary == "Basic256Sha256/SignAndEncrypt"

    asyncio.run(scenario())


def test_connect_error_redacts_both_secrets(monkeypatch, tmp_path) -> None:
    class FailingClient(_FakeClient):
        async def connect(self, **kwargs) -> None:
            raise RuntimeError("login rejected plc-secret key-secret")

    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (FailingClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    config = _secure_config(tmp_path)

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        with pytest.raises(LiveConnectionError) as info:
            await client.connect()
        text = str(info.value)
        assert "plc-secret" not in text
        assert "key-secret" not in text
        assert text.count("<redacted>") == 2

    asyncio.run(scenario())


def test_security_setup_error_redacts_private_key_password(monkeypatch, tmp_path) -> None:
    class FailingSecurityClient(_FakeClient):
        async def set_security(self, *args, **kwargs) -> None:
            raise RuntimeError("bad encrypted key key-secret")

    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (FailingSecurityClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    config = _secure_config(tmp_path)

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        with pytest.raises(LiveConnectionError) as info:
            await client.connect()
        assert "key-secret" not in str(info.value)

    asyncio.run(scenario())


def test_read_only_surface_survives_security_change() -> None:
    client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/")
    for prohibited in ("write", "write_value", "set_value", "call_method", "force", "reset"):
        assert not hasattr(client, prohibited)


def test_cli_reads_passwords_only_from_environment(monkeypatch, tmp_path) -> None:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    monkeypatch.setenv("PLC_PASSWORD", "pw-secret")
    monkeypatch.setenv("PLC_KEY_PASSWORD", "key-secret")
    parser = argparse.ArgumentParser()
    add_security_args(parser)
    args = parser.parse_args(
        [
            "--username",
            "operator",
            "--password-env",
            "PLC_PASSWORD",
            "--security-policy",
            "Basic256Sha256",
            "--security-mode",
            "SignAndEncrypt",
            "--client-certificate",
            certificate,
            "--client-private-key",
            private_key,
            "--private-key-password-env",
            "PLC_KEY_PASSWORD",
            "--server-certificate",
            server_certificate,
        ]
    )
    config = security_from_args(args)
    assert config.password == "pw-secret"
    assert config.private_key_password == "key-secret"
    assert "pw-secret" not in repr(config)


def test_cli_has_no_literal_password_option() -> None:
    parser = argparse.ArgumentParser()
    add_security_args(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--password", "do-not-allow"])


def test_missing_secret_environment_variable_fails_closed() -> None:
    parser = argparse.ArgumentParser()
    add_security_args(parser)
    args = parser.parse_args(["--username", "operator", "--password-env", "MISSING_ENV"])
    with pytest.raises(LiveConfigurationError, match="not set"):
        security_from_args(args)
