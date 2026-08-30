from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import pytest

from devagent.live.cli_security import add_security_args, security_from_args
from devagent.live.errors import LiveConfigurationError, LiveConnectionError
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import (
    DEPRECATED_SECURITY_POLICIES,
    LiveSecurityConfig,
    canonical_security_policy_name,
    validate_opcua_endpoint,
)
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
        self.user_certificate_call = None
        self.user_private_key_call = None
        type(self).instances.append(self)

    def set_user(self, value: str) -> None:
        self.user = value

    def set_password(self, value: str) -> None:
        self.password = value

    async def set_security(self, *args, **kwargs) -> None:
        self.security_call = (args, kwargs)

    async def load_client_certificate(self, path: str) -> None:
        self.user_certificate_call = path

    async def load_private_key(self, path: str, password=None) -> None:
        self.user_private_key_call = (path, password)

    async def connect(self, **kwargs) -> None:
        self.connect_call = kwargs

    async def disconnect(self) -> None:
        return None


class _Policies:
    SecurityPolicyBasic128Rsa15 = object()
    SecurityPolicyBasic256 = object()
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


def _user_security_files(tmp_path) -> tuple[str, str]:
    certificate = tmp_path / "user.der"
    private_key = tmp_path / "user.pem"
    certificate.write_text("test", encoding="utf-8")
    private_key.write_text("test", encoding="utf-8")
    return str(certificate), str(private_key)


def _secure_config(
    tmp_path,
    *,
    mode: str = "SignAndEncrypt",
    policy: str = "Basic256Sha256",
) -> LiveSecurityConfig:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    return LiveSecurityConfig(
        username="operator",
        password="plc-secret",
        security_policy=policy,
        security_mode=mode,
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


def test_username_password_over_no_security_is_blocked() -> None:
    with pytest.raises(LiveConfigurationError, match="NoSecurity"):
        LiveSecurityConfig(username="operator", password="pw")


def test_sign_only_accepts_username_password_on_secure_channel(tmp_path) -> None:
    config = _secure_config(tmp_path, mode="Sign")
    assert config.authentication_mode == "USERNAME_PASSWORD"
    assert config.channel_summary == "Basic256Sha256/Sign"


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


def test_certificate_paths_are_expanded_before_asyncua_uses_them(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("client.der", "client.pem", "server.der", "user.der", "user.pem"):
        (home / name).write_text("test", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate="~/client.der",
        client_private_key="~/client.pem",
        server_certificate="~/server.der",
        user_certificate="~/user.der",
        user_private_key="~/user.pem",
    )
    assert config.client_certificate == str(home / "client.der")
    assert config.client_private_key == str(home / "client.pem")
    assert config.server_certificate == str(home / "server.der")
    assert config.user_certificate == str(home / "user.der")
    assert config.user_private_key == str(home / "user.pem")
    config.validate_files()


@pytest.mark.parametrize("policy", DEPRECATED_SECURITY_POLICIES)
def test_legacy_policies_are_explicit_deprecated_compatibility(tmp_path, policy: str) -> None:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    config = LiveSecurityConfig(
        security_policy=policy,
        security_mode="SignAndEncrypt",
        client_certificate=certificate,
        client_private_key=private_key,
        server_certificate=server_certificate,
    )
    assert config.deprecated_policy is True
    assert "[DEPRECATED]" in config.channel_summary


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [
        ("Aes128_Sha256_RsaOaep", "Aes128Sha256RsaOaep"),
        ("http://opcfoundation.org/UA/SecurityPolicy#Aes256_Sha256_RsaPss", "Aes256Sha256RsaPss"),
        ("ECC_nistP256", "EccNistP256"),
    ],
)
def test_security_policy_names_are_normalized(input_value: str, expected: str) -> None:
    assert canonical_security_policy_name(input_value) == expected


def test_ecc_profile_is_recognized_but_runtime_unavailable(tmp_path) -> None:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    with pytest.raises(LiveConfigurationError, match="recognized by the standard"):
        LiveSecurityConfig(
            security_policy="EccNistP256",
            security_mode="SignAndEncrypt",
            client_certificate=certificate,
            client_private_key=private_key,
            server_certificate=server_certificate,
        )


def test_x509_user_identity_requires_certificate_and_key(tmp_path) -> None:
    user_certificate, _user_private_key = _user_security_files(tmp_path)
    with pytest.raises(LiveConfigurationError, match="must be configured together"):
        LiveSecurityConfig(user_certificate=user_certificate)


def test_x509_user_identity_and_username_are_mutually_exclusive(tmp_path) -> None:
    app_certificate, app_private_key, server_certificate = _security_files(tmp_path)
    user_certificate, user_private_key = _user_security_files(tmp_path)
    with pytest.raises(LiveConfigurationError, match="exactly one"):
        LiveSecurityConfig(
            username="operator",
            password="pw",
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=app_certificate,
            client_private_key=app_private_key,
            server_certificate=server_certificate,
            user_certificate=user_certificate,
            user_private_key=user_private_key,
        )


def test_x509_user_identity_is_separate_from_application_certificate(tmp_path) -> None:
    app_certificate, app_private_key, server_certificate = _security_files(tmp_path)
    user_certificate, user_private_key = _user_security_files(tmp_path)
    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=app_certificate,
        client_private_key=app_private_key,
        server_certificate=server_certificate,
        user_certificate=user_certificate,
        user_private_key=user_private_key,
        user_private_key_password="user-key-secret",
    )
    assert config.authentication_mode == "X509_USER_CERTIFICATE"
    config.validate_files()
    assert "user-key-secret" not in repr(config)
    assert config.redact("user-key-secret") == "<redacted>"


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


@pytest.mark.parametrize(
    ("policy", "expected_policy"),
    [
        ("Basic128Rsa15", _Policies.SecurityPolicyBasic128Rsa15),
        ("Basic256", _Policies.SecurityPolicyBasic256),
    ],
)
def test_legacy_policy_connects_through_asyncua_compatibility_surface(
    monkeypatch, tmp_path, policy: str, expected_policy: object
) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (_FakeClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    config = _secure_config(tmp_path, policy=policy)

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        await client.connect()
        raw = _FakeClient.instances[-1]
        assert raw.security_call[0][0] is expected_policy
        assert "[DEPRECATED]" in client.security_summary

    asyncio.run(scenario())


def test_x509_user_identity_is_loaded_into_asyncua_client(monkeypatch, tmp_path) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (_FakeClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    app_certificate, app_private_key, server_certificate = _security_files(tmp_path)
    user_certificate, user_private_key = _user_security_files(tmp_path)
    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=app_certificate,
        client_private_key=app_private_key,
        server_certificate=server_certificate,
        user_certificate=user_certificate,
        user_private_key=user_private_key,
        user_private_key_password="user-key-secret",
    )

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        await client.connect()
        raw = _FakeClient.instances[-1]
        assert raw.user is None
        assert raw.password is None
        assert raw.user_certificate_call.endswith("user.der")
        assert raw.user_private_key_call[0].endswith("user.pem")
        assert raw.user_private_key_call[1] == "user-key-secret"
        assert client.authentication_mode == "X509_USER_CERTIFICATE"

    asyncio.run(scenario())


def test_x509_user_identity_fails_closed_if_runtime_loader_missing(monkeypatch, tmp_path) -> None:
    class MissingUserCertificateClient(_FakeClient):
        load_client_certificate = None

    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (MissingUserCertificateClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    user_certificate, user_private_key = _user_security_files(tmp_path)
    config = LiveSecurityConfig(
        user_certificate=user_certificate,
        user_private_key=user_private_key,
    )

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        with pytest.raises(LiveConnectionError, match="X.509 user-certificate"):
            await client.connect()

    asyncio.run(scenario())


def test_connect_error_redacts_password_and_application_key_secret(monkeypatch, tmp_path) -> None:
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


def test_read_only_surface_survives_security_change() -> None:
    client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/")
    for prohibited in ("write", "write_value", "set_value", "call_method", "force", "reset"):
        assert not hasattr(client, prohibited)


def test_cli_reads_x509_key_password_only_from_environment(monkeypatch, tmp_path) -> None:
    certificate, private_key, server_certificate = _security_files(tmp_path)
    user_certificate, user_private_key = _user_security_files(tmp_path)
    monkeypatch.setenv("PLC_KEY_PASSWORD", "key-secret")
    monkeypatch.setenv("PLC_USER_KEY_PASSWORD", "user-key-secret")
    parser = argparse.ArgumentParser()
    add_security_args(parser)
    args = parser.parse_args(
        [
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
            "--user-certificate",
            user_certificate,
            "--user-private-key",
            user_private_key,
            "--user-private-key-password-env",
            "PLC_USER_KEY_PASSWORD",
        ]
    )
    config = security_from_args(args)
    assert config.private_key_password == "key-secret"
    assert config.user_private_key_password == "user-key-secret"
    assert config.authentication_mode == "X509_USER_CERTIFICATE"
    assert "key-secret" not in repr(config)
    assert "user-key-secret" not in repr(config)


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
