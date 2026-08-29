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
    return f"opc.tcp://127.0.0.1:{port}/devagent/secure-simulator/"


def _create_application_certificate(
    private_key_path,
    certificate_path,
    application_uri: str,
    *,
    server: bool,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Qualification"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                "DevAgent Secure Simulator" if server else "DevAgent Secure Client",
            ),
        ]
    )
    extended_usage = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        extended_usage.append(ExtendedKeyUsageOID.SERVER_AUTH)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
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
        .sign(key, hashes.SHA256())
    )

    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.DER))


def _client_security(
    *,
    username: str,
    password: str,
    client_certificate: str,
    client_private_key: str,
    server_certificate: str,
    application_uri: str,
) -> LiveSecurityConfig:
    return LiveSecurityConfig(
        username=username,
        password=password,
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate=client_certificate,
        client_private_key=client_private_key,
        server_certificate=server_certificate,
        application_uri=application_uri,
    )


def test_secure_username_password_and_server_certificate_pinning(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        server_key = tmp_path / "server-key.pem"
        server_cert = tmp_path / "server-cert.der"
        wrong_server_key = tmp_path / "wrong-server-key.pem"
        wrong_server_cert = tmp_path / "wrong-server-cert.der"
        client_key = tmp_path / "client-key.pem"
        client_cert = tmp_path / "client-cert.der"
        client_uri = "urn:devagent:qualification:secure-client"

        _create_application_certificate(
            server_key,
            server_cert,
            OpcUaSimulator.APPLICATION_URI,
            server=True,
        )
        _create_application_certificate(
            wrong_server_key,
            wrong_server_cert,
            "urn:devagent:qualification:wrong-server",
            server=True,
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

            good = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=_client_security(
                    username="operator",
                    password="correct-password",
                    client_certificate=str(client_cert),
                    client_private_key=str(client_key),
                    server_certificate=str(server_cert),
                    application_uri=client_uri,
                ),
            )
            await good.connect()
            try:
                state = await good.read(simulator.node_ids.machine_state)
                assert state.value == "BLOCKED"
                assert state.quality is Quality.GOOD
                assert state.trust is TrustState.CURRENT
                assert good.authentication_mode == "USERNAME_PASSWORD"
                assert good.security_summary == "Basic256Sha256/SignAndEncrypt"
            finally:
                await good.disconnect()

            wrong_password = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=_client_security(
                    username="operator",
                    password="wrong-password",
                    client_certificate=str(client_cert),
                    client_private_key=str(client_key),
                    server_certificate=str(server_cert),
                    application_uri=client_uri,
                ),
            )
            with pytest.raises(LiveConnectionError) as bad_password_error:
                await wrong_password.connect()
            assert "wrong-password" not in str(bad_password_error.value)
            assert bad_password_error.value.__cause__ is None

            wrong_pin = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=_client_security(
                    username="operator",
                    password="correct-password",
                    client_certificate=str(client_cert),
                    client_private_key=str(client_key),
                    server_certificate=str(wrong_server_cert),
                    application_uri=client_uri,
                ),
            )
            with pytest.raises(LiveConnectionError):
                await wrong_pin.connect()

            anonymous = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
            with pytest.raises(LiveConnectionError):
                await anonymous.connect()

    asyncio.run(scenario())
