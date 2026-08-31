from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

pytest.importorskip("asyncua")

from devagent.live.errors import LiveConnectionError
from devagent.live.models import Quality, TrustState
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/trust-store-simulator/"


def _write_key(path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _create_ca(private_key_path, certificate_path, common_name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Trust Qualification"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write_key(private_key_path, key)
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    return key, certificate


def _create_application_certificate(
    private_key_path,
    certificate_path,
    application_uri: str,
    *,
    server: bool,
    issuer_key=None,
    issuer_certificate=None,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Trust Qualification"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                "DevAgent Trust Server" if server else "DevAgent Trust Client",
            ),
        ]
    )
    issuer_name = issuer_certificate.subject if issuer_certificate is not None else subject
    signing_key = issuer_key if issuer_key is not None else key
    extended_usage = [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
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
        .add_extension(x509.ExtendedKeyUsage(extended_usage), critical=False)
        .sign(signing_key, hashes.SHA256())
    )
    _write_key(private_key_path, key)
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.DER))


def test_ca_trust_store_connects_without_per_server_pin_and_rejects_wrong_ca(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        trusted_dir = tmp_path / "trusted"
        trusted_dir.mkdir()
        wrong_trusted_dir = tmp_path / "wrong-trusted"
        wrong_trusted_dir.mkdir()

        ca_key_path = tmp_path / "factory-ca-key.pem"
        ca_cert_path = trusted_dir / "factory-ca.der"
        ca_key, ca_cert = _create_ca(ca_key_path, ca_cert_path, "Factory OPC UA CA")

        wrong_ca_key_path = tmp_path / "wrong-ca-key.pem"
        wrong_ca_cert_path = wrong_trusted_dir / "wrong-ca.der"
        _create_ca(wrong_ca_key_path, wrong_ca_cert_path, "Wrong Factory CA")

        server_key = tmp_path / "server-key.pem"
        server_cert = tmp_path / "server-cert.der"
        client_key = tmp_path / "client-key.pem"
        client_cert = tmp_path / "client-cert.der"
        client_uri = "urn:devagent:qualification:trust-store-client"

        _create_application_certificate(
            server_key,
            server_cert,
            OpcUaSimulator.APPLICATION_URI,
            server=True,
            issuer_key=ca_key,
            issuer_certificate=ca_cert,
        )
        _create_application_certificate(
            client_key,
            client_cert,
            client_uri,
            server=False,
        )

        async with OpcUaSimulator(
            endpoint,
            scenario="blocker",
            username="operator",
            password="correct-password",
            server_certificate=str(server_cert),
            server_private_key=str(server_key),
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
        ) as simulator:
            assert simulator.node_ids is not None

            trusted = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=LiveSecurityConfig(
                    username="operator",
                    password="correct-password",
                    security_policy="Basic256Sha256",
                    security_mode="SignAndEncrypt",
                    client_certificate=str(client_cert),
                    client_private_key=str(client_key),
                    trust_store=str(trusted_dir),
                    application_uri=client_uri,
                ),
            )
            await trusted.connect()
            try:
                state = await trusted.read(simulator.node_ids.machine_state)
                assert state.value == "BLOCKED"
                assert state.quality is Quality.GOOD
                assert state.trust is TrustState.CURRENT
                assert trusted.security.server_certificate is None
                assert trusted.security.server_trust_mode == "TRUST_STORE"
            finally:
                await trusted.disconnect()

            untrusted = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=LiveSecurityConfig(
                    username="operator",
                    password="correct-password",
                    security_policy="Basic256Sha256",
                    security_mode="SignAndEncrypt",
                    client_certificate=str(client_cert),
                    client_private_key=str(client_key),
                    trust_store=str(wrong_trusted_dir),
                    application_uri=client_uri,
                ),
            )
            with pytest.raises(LiveConnectionError):
                await untrusted.connect()

    asyncio.run(scenario())
