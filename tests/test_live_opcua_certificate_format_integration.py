from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

pytest.importorskip("asyncua")

from devagent.live.models import Quality, TrustState
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/certificate-format-simulator/"


def _write_key(path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _create_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Format Qualification"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Factory OPC UA CA"),
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
    return key, certificate


def _create_application_certificate(
    application_uri: str,
    *,
    server: bool,
    issuer_key: rsa.RSAPrivateKey | None = None,
    issuer_certificate: x509.Certificate | None = None,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Format Qualification"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                "DevAgent Format Server" if server else "DevAgent Format Client",
            ),
        ]
    )
    issuer_name = issuer_certificate.subject if issuer_certificate is not None else subject
    signing_key = issuer_key if issuer_key is not None else key
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
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .sign(signing_key, hashes.SHA256())
    )
    return key, certificate


def test_pkcs12_client_and_pem_crt_ca_connect_to_real_server(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        trusted = tmp_path / "trusted"
        trusted.mkdir()

        ca_key, ca_cert = _create_ca()
        (trusted / "factory-ca.crt").write_bytes(
            ca_cert.public_bytes(serialization.Encoding.PEM)
        )

        server_key, server_cert = _create_application_certificate(
            OpcUaSimulator.APPLICATION_URI,
            server=True,
            issuer_key=ca_key,
            issuer_certificate=ca_cert,
        )
        server_key_path = tmp_path / "server-key.pem"
        server_cert_path = tmp_path / "server-cert.der"
        _write_key(server_key_path, server_key)
        server_cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.DER))

        client_uri = "urn:devagent:qualification:pkcs12-client"
        client_key, client_cert = _create_application_certificate(client_uri, server=False)
        client_bundle = tmp_path / "devagent-client.pfx"
        client_bundle.write_bytes(
            pkcs12.serialize_key_and_certificates(
                name=b"devagent-client",
                key=client_key,
                cert=client_cert,
                cas=None,
                encryption_algorithm=serialization.BestAvailableEncryption(b"bundle-secret"),
            )
        )

        async with OpcUaSimulator(
            endpoint,
            scenario="blocker",
            username="operator",
            password="correct-password",
            server_certificate=str(server_cert_path),
            server_private_key=str(server_key_path),
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
        ) as simulator:
            assert simulator.node_ids is not None
            client = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=LiveSecurityConfig(
                    username="operator",
                    password="correct-password",
                    security_policy="Basic256Sha256",
                    security_mode="SignAndEncrypt",
                    client_certificate=str(client_bundle),
                    private_key_password="bundle-secret",
                    trust_store=str(trusted),
                    application_uri=client_uri,
                ),
            )
            await client.connect()
            try:
                state = await client.read(simulator.node_ids.machine_state)
                assert state.value == "BLOCKED"
                assert state.quality is Quality.GOOD
                assert state.trust is TrustState.CURRENT
                assert client.security.client_private_key is None
                assert client.security.server_trust_mode == "TRUST_STORE"
            finally:
                await client.disconnect()

    asyncio.run(scenario())
