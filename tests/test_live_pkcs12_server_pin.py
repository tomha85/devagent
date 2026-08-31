from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from devagent.live import opcua_client
from devagent.live.errors import LiveConnectionError
from devagent.live.opcua_client import _prepare_server_pin
from devagent.live.security import LiveSecurityConfig


class _FakeCertProperties:
    def __init__(self, path_or_content, extension=None, password=None) -> None:
        self.path_or_content = path_or_content
        self.extension = extension
        self.password = password


def _certificate(common_name: str) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )


def _certificate_only_bundle(path: Path, certificates: list[x509.Certificate], password: str) -> None:
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            name=None,
            key=None,
            cert=None,
            cas=certificates,
            encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
        )
    )


def _config(tmp_path: Path, server_bundle: Path, password: str) -> LiveSecurityConfig:
    client_cert = tmp_path / "client.der"
    client_key = tmp_path / "client.pem"
    client_cert.write_text("placeholder", encoding="utf-8")
    client_key.write_text("placeholder", encoding="utf-8")
    return LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=str(client_cert),
        client_private_key=str(client_key),
        server_certificate=str(server_bundle),
        server_certificate_password=password,
    )


def test_certificate_only_pkcs12_server_pin_is_accepted(monkeypatch, tmp_path: Path) -> None:
    expected = _certificate("PLC Server")
    bundle = tmp_path / "plc-server.pfx"
    _certificate_only_bundle(bundle, [expected], "server-secret")
    monkeypatch.setattr(
        opcua_client,
        "_require_uacrypto",
        lambda: SimpleNamespace(CertProperties=_FakeCertProperties),
    )

    pin = _prepare_server_pin(_config(tmp_path, bundle, "server-secret"))

    assert isinstance(pin, _FakeCertProperties)
    assert pin.extension == "der"
    actual = x509.load_der_x509_certificate(pin.path_or_content)
    assert actual.fingerprint(hashes.SHA256()) == expected.fingerprint(hashes.SHA256())


def test_certificate_only_pkcs12_server_pin_rejects_ambiguous_multiple_certificates(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "ambiguous-server.p12"
    _certificate_only_bundle(
        bundle,
        [_certificate("PLC Server A"), _certificate("PLC Server B")],
        "server-secret",
    )
    monkeypatch.setattr(
        opcua_client,
        "_require_uacrypto",
        lambda: SimpleNamespace(CertProperties=_FakeCertProperties),
    )

    with pytest.raises(LiveConnectionError, match="multiple certificates and no unique primary"):
        _prepare_server_pin(_config(tmp_path, bundle, "server-secret"))
