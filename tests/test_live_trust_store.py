from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from devagent.live import opcua_client
from devagent.live.cli_security import add_security_args, security_from_args
from devagent.live.commission import _security_from_json
from devagent.live.errors import LiveConfigurationError
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig


class _State:
    value = "connected"


class _FakeMode:
    Sign = object()
    SignAndEncrypt = object()


class _Policies:
    SecurityPolicyBasic256Sha256 = object()


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.state = _State()
        self.application_uri = None
        self.security_call = None
        self.connect_call = None
        self.certificate_validator = None
        type(self).instances.append(self)

    def set_user(self, _value: str) -> None:
        return None

    def set_password(self, _value: str) -> None:
        return None

    async def set_security(self, *args, **kwargs) -> None:
        self.security_call = (args, kwargs)

    async def connect(self, **kwargs) -> None:
        self.connect_call = kwargs

    async def disconnect(self) -> None:
        return None


class _FakeTrustStore:
    instances: list["_FakeTrustStore"] = []

    def __init__(self, trust_locations: list[Path], crl_locations: list[Path]) -> None:
        self.trust_locations = trust_locations
        self.crl_locations = crl_locations
        self.loaded = False
        type(self).instances.append(self)

    async def load(self) -> None:
        self.loaded = True


class _FakeValidator:
    instances: list["_FakeValidator"] = []

    def __init__(self, options, trust_store) -> None:
        self.options = options
        self.trust_store = trust_store
        self.calls: list[tuple[object, object]] = []
        type(self).instances.append(self)

    async def __call__(self, certificate: object, app_description: object) -> None:
        self.calls.append((certificate, app_description))


class _FakeValidatorOptions:
    TRUSTED_VALIDATION = 1
    PEER_SERVER = 2


def _secure_files(tmp_path: Path) -> tuple[str, str]:
    cert = tmp_path / "client.der"
    key = tmp_path / "client-key.pem"
    cert.write_text("client-cert", encoding="utf-8")
    key.write_text("client-key", encoding="utf-8")
    return str(cert), str(key)


def _trust_dir(tmp_path: Path, name: str = "trusted") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "factory-ca.der").write_text("trusted-cert", encoding="utf-8")
    return directory


def test_secure_config_accepts_trust_store_without_server_pin(tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    trust_store = _trust_dir(tmp_path)
    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=client_cert,
        client_private_key=client_key,
        trust_store=str(trust_store),
    )
    config.validate_files()
    assert config.server_certificate is None
    assert config.server_trust_mode == "TRUST_STORE"


def test_secure_config_requires_pin_or_trust_store(tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    with pytest.raises(LiveConfigurationError, match="pinned server certificate or a trust store"):
        LiveSecurityConfig(
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=client_cert,
            client_private_key=client_key,
        )


def test_crl_store_requires_trust_store(tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    crl_dir = tmp_path / "crl"
    crl_dir.mkdir()
    with pytest.raises(LiveConfigurationError, match="--crl-store requires --trust-store"):
        LiveSecurityConfig(
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=client_cert,
            client_private_key=client_key,
            server_certificate=str(tmp_path / "server.der"),
            crl_store=str(crl_dir),
        )


def test_empty_trust_store_fails_closed(tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    trust_store = tmp_path / "trusted"
    trust_store.mkdir()
    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=client_cert,
        client_private_key=client_key,
        trust_store=str(trust_store),
    )
    with pytest.raises(LiveConfigurationError, match="contains no supported trusted certificates"):
        config.validate_files()


def test_trust_store_is_wired_to_asyncua_validator(monkeypatch, tmp_path: Path) -> None:
    _FakeClient.instances.clear()
    _FakeTrustStore.instances.clear()
    _FakeValidator.instances.clear()
    client_cert, client_key = _secure_files(tmp_path)
    trust_store = _trust_dir(tmp_path)
    crl_store = tmp_path / "crl"
    crl_store.mkdir()
    (crl_store / "factory.crl.der").write_text("crl", encoding="utf-8")
    hostname_calls: list[tuple[object, str]] = []

    monkeypatch.setattr(
        opcua_client,
        "_require_asyncua",
        lambda: (_FakeClient, SimpleNamespace(MessageSecurityMode=_FakeMode)),
    )
    monkeypatch.setattr(opcua_client, "_require_security_policies", lambda: _Policies)
    monkeypatch.setattr(
        opcua_client,
        "_require_trust_runtime",
        lambda: (_FakeTrustStore, _FakeValidator, _FakeValidatorOptions),
    )
    monkeypatch.setattr(
        opcua_client,
        "_validate_server_certificate_hostname",
        lambda certificate, endpoint: hostname_calls.append((certificate, endpoint)),
    )

    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=client_cert,
        client_private_key=client_key,
        trust_store=str(trust_store),
        crl_store=str(crl_store),
    )

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", security=config)
        await client.connect()
        raw = _FakeClient.instances[-1]
        trust = _FakeTrustStore.instances[-1]
        validator = _FakeValidator.instances[-1]
        assert trust.loaded is True
        assert trust.trust_locations == [trust_store]
        assert trust.crl_locations == [crl_store]
        assert validator.options == 3
        assert validator.trust_store is trust
        assert callable(raw.certificate_validator)
        certificate = object()
        app_description = object()
        await raw.certificate_validator(certificate, app_description)
        assert validator.calls == [(certificate, app_description)]
        assert hostname_calls == [(certificate, "opc.tcp://127.0.0.1:4840/")]
        assert raw.security_call[1]["server_certificate"] is None

    asyncio.run(scenario())


def test_cli_maps_trust_store_and_crl_store(monkeypatch, tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    trust_store = _trust_dir(tmp_path)
    crl_store = tmp_path / "crl"
    crl_store.mkdir()
    (crl_store / "factory.crl.der").write_text("crl", encoding="utf-8")

    parser = argparse.ArgumentParser(allow_abbrev=False)
    add_security_args(parser)
    args = parser.parse_args(
        [
            "--security-policy",
            "Basic256Sha256",
            "--security-mode",
            "SignAndEncrypt",
            "--client-certificate",
            client_cert,
            "--client-private-key",
            client_key,
            "--trust-store",
            str(trust_store),
            "--crl-store",
            str(crl_store),
        ]
    )
    config = security_from_args(args)
    assert config.trust_store == str(trust_store)
    assert config.crl_store == str(crl_store)


def test_commission_json_maps_relative_trust_store(tmp_path: Path) -> None:
    client_cert, client_key = _secure_files(tmp_path)
    trust_store = _trust_dir(tmp_path)
    raw = {
        "security_policy": "Basic256Sha256",
        "security_mode": "SignAndEncrypt",
        "client_certificate": Path(client_cert).name,
        "client_private_key": Path(client_key).name,
        "trust_store": trust_store.name,
    }
    config = _security_from_json(
        raw,
        base_dir=tmp_path,
        env={},
        validate_files=True,
    )
    assert config.trust_store == str(trust_store.resolve())
    assert config.server_certificate is None
