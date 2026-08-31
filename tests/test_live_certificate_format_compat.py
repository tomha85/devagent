from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from devagent.live.certificate_material import (
    certificate_encoding,
    load_certificate_objects,
)
from devagent.live.cli_security import add_security_args, security_from_args
from devagent.live.commission import _security_from_json
from devagent.live.errors import LiveConfigurationError
from devagent.live.security import LiveSecurityConfig


def _certificate(common_name: str = "DevAgent Test") -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _write_pkcs12(path: Path, *, password: str = "bundle-secret") -> None:
    key, certificate = _certificate(path.stem)
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            name=path.stem.encode("utf-8"),
            key=key,
            cert=certificate,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
        )
    )


def test_cer_and_crt_are_detected_by_content_not_suffix(tmp_path: Path) -> None:
    _key, certificate = _certificate()
    pem_crt = tmp_path / "server.crt"
    pem_crt.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    der_cer = tmp_path / "server.cer"
    der_cer.write_bytes(certificate.public_bytes(serialization.Encoding.DER))

    assert certificate_encoding(pem_crt) == "pem"
    assert certificate_encoding(der_cer) == "der"
    assert load_certificate_objects(pem_crt)[0].serial_number == certificate.serial_number
    assert load_certificate_objects(der_cer)[0].serial_number == certificate.serial_number


@pytest.mark.parametrize("suffix", [".pfx", ".p12"])
def test_client_pkcs12_bundle_replaces_separate_private_key(tmp_path: Path, suffix: str) -> None:
    bundle = tmp_path / f"devagent{suffix}"
    _write_pkcs12(bundle)
    server = tmp_path / "server.der"
    server.write_text("placeholder", encoding="utf-8")

    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=str(bundle),
        private_key_password="bundle-secret",
        server_certificate=str(server),
    )
    config.validate_files()
    assert config.client_private_key is None


@pytest.mark.parametrize("suffix", [".pfx", ".p12"])
def test_user_pkcs12_bundle_replaces_separate_private_key(tmp_path: Path, suffix: str) -> None:
    bundle = tmp_path / f"operator{suffix}"
    _write_pkcs12(bundle)
    config = LiveSecurityConfig(
        user_certificate=str(bundle),
        user_private_key_password="bundle-secret",
    )
    config.validate_files()
    assert config.authentication_mode == "X509_USER_CERTIFICATE"
    assert config.user_private_key is None


def test_separate_key_is_rejected_when_certificate_is_pkcs12_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "devagent.pfx"
    _write_pkcs12(bundle)
    key = tmp_path / "client.pem"
    key.write_text("placeholder", encoding="utf-8")
    server = tmp_path / "server.der"
    server.write_text("placeholder", encoding="utf-8")

    with pytest.raises(LiveConfigurationError, match="Do not provide --client-private-key"):
        LiveSecurityConfig(
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=str(bundle),
            client_private_key=str(key),
            private_key_password="bundle-secret",
            server_certificate=str(server),
        )


def test_trust_store_accepts_all_customer_certificate_suffixes(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    key, certificate = _certificate("Factory CA")
    (trusted / "ca.der").write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    (trusted / "ca.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (trusted / "ca.cer").write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    (trusted / "ca.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    for suffix in (".pfx", ".p12"):
        (trusted / f"ca{suffix}").write_bytes(
            pkcs12.serialize_key_and_certificates(
                name=b"factory-ca",
                key=key,
                cert=certificate,
                cas=None,
                encryption_algorithm=serialization.BestAvailableEncryption(b"trust-secret"),
            )
        )

    client_cert = tmp_path / "client.der"
    client_key = tmp_path / "client.pem"
    client_cert.write_text("placeholder", encoding="utf-8")
    client_key.write_text("placeholder", encoding="utf-8")
    config = LiveSecurityConfig(
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=str(client_cert),
        client_private_key=str(client_key),
        trust_store=str(trusted),
        trust_store_password="trust-secret",
    )
    config.validate_files()


def test_cli_maps_server_and_trust_bundle_passwords(monkeypatch, tmp_path: Path) -> None:
    client = tmp_path / "client.pfx"
    server = tmp_path / "server.p12"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _write_pkcs12(client, password="client-secret")
    _write_pkcs12(server, password="server-secret")
    _write_pkcs12(trusted / "ca.pfx", password="trust-secret")

    monkeypatch.setenv("CLIENT_PFX_PASSWORD", "client-secret")
    monkeypatch.setenv("SERVER_PFX_PASSWORD", "server-secret")
    monkeypatch.setenv("TRUST_PFX_PASSWORD", "trust-secret")

    parser = argparse.ArgumentParser(allow_abbrev=False)
    add_security_args(parser)
    args = parser.parse_args(
        [
            "--security-policy",
            "Basic256Sha256",
            "--security-mode",
            "SignAndEncrypt",
            "--client-certificate",
            str(client),
            "--private-key-password-env",
            "CLIENT_PFX_PASSWORD",
            "--server-certificate",
            str(server),
            "--server-certificate-password-env",
            "SERVER_PFX_PASSWORD",
            "--trust-store",
            str(trusted),
            "--trust-store-password-env",
            "TRUST_PFX_PASSWORD",
        ]
    )
    config = security_from_args(args)
    assert config.private_key_password == "client-secret"
    assert config.server_certificate_password == "server-secret"
    assert config.trust_store_password == "trust-secret"
    rendered = repr(config)
    assert "client-secret" not in rendered
    assert "server-secret" not in rendered
    assert "trust-secret" not in rendered


def test_commission_json_maps_pkcs12_password_environment_references(tmp_path: Path) -> None:
    client = tmp_path / "client.pfx"
    server = tmp_path / "server.p12"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _write_pkcs12(client, password="client-secret")
    _write_pkcs12(server, password="server-secret")
    _write_pkcs12(trusted / "ca.pfx", password="trust-secret")

    config = _security_from_json(
        {
            "security_policy": "Basic256Sha256",
            "security_mode": "SignAndEncrypt",
            "client_certificate": client.name,
            "private_key_password_env": "CLIENT_PFX_PASSWORD",
            "server_certificate": server.name,
            "server_certificate_password_env": "SERVER_PFX_PASSWORD",
            "trust_store": trusted.name,
            "trust_store_password_env": "TRUST_PFX_PASSWORD",
        },
        base_dir=tmp_path,
        env={
            "CLIENT_PFX_PASSWORD": "client-secret",
            "SERVER_PFX_PASSWORD": "server-secret",
            "TRUST_PFX_PASSWORD": "trust-secret",
        },
        validate_files=True,
    )
    assert config.client_certificate == str(client.resolve())
    assert config.client_private_key is None
    assert config.server_certificate_password == "server-secret"
    assert config.trust_store_password == "trust-secret"
