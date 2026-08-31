from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

pytest.importorskip("asyncua")

from asyncua import Server, ua
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.user_managers import CertificateUserManager

from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig


def _free_endpoint(path: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/{path.strip('/')}/"


def _create_certificate(
    private_key_path,
    certificate_path,
    uri: str,
    common_name: str,
    *,
    server: bool = False,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DevAgent Qualification"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
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
                    x509.UniformResourceIdentifier(uri),
                    x509.DNSName(socket.gethostname()),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
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


class _StaticUsernameManager:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def get_user(
        self,
        iserver,
        username: str | None = None,
        password: str | None = None,
        certificate=None,
    ):
        if username is None or password is None:
            return None
        if not hmac.compare_digest(username, self.username):
            return None
        if not hmac.compare_digest(password, self.password):
            return None
        return User(role=UserRole.User, name=username)


async def _build_server(
    *,
    endpoint: str,
    server_uri: str,
    server_certificate: str,
    server_private_key: str,
    security_policy_type,
    identity_token_type,
    user_manager,
):
    server = Server(user_manager=user_manager)
    await server.init()
    await server.set_application_uri(server_uri)
    server.set_endpoint(endpoint)
    server.set_server_name("DevAgent OPC UA Profile Qualification")
    server.set_security_policy([security_policy_type])
    server.set_identity_tokens([identity_token_type])
    await server.load_certificate(server_certificate)
    await server.load_private_key(server_private_key)
    namespace = await server.register_namespace("urn:devagent:profile:qualification")
    node = await server.nodes.objects.add_variable(
        ua.NodeId("Profile.ReadOnlyValue", namespace),
        ua.QualifiedName("ReadOnlyValue", namespace),
        42,
        varianttype=ua.VariantType.Int32,
    )
    await node.set_read_only()
    await server.start()
    return server, node.nodeid.to_string()


def test_username_password_connects_over_sign_channel(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint("devagent/sign-user")
        server_uri = "urn:devagent:qualification:sign-server"
        client_uri = "urn:devagent:qualification:sign-client"
        server_key = tmp_path / "sign-server-key.pem"
        server_cert = tmp_path / "sign-server.der"
        client_key = tmp_path / "sign-client-key.pem"
        client_cert = tmp_path / "sign-client.der"
        _create_certificate(server_key, server_cert, server_uri, "Sign Server", server=True)
        _create_certificate(client_key, client_cert, client_uri, "Sign Client")

        server, node_id = await _build_server(
            endpoint=endpoint,
            server_uri=server_uri,
            server_certificate=str(server_cert),
            server_private_key=str(server_key),
            security_policy_type=ua.SecurityPolicyType.Basic256Sha256_Sign,
            identity_token_type=ua.UserNameIdentityToken,
            user_manager=_StaticUsernameManager("operator", "correct-password"),
        )
        try:
            config = LiveSecurityConfig(
                username="operator",
                password="correct-password",
                security_policy="Basic256Sha256",
                security_mode="Sign",
                client_certificate=str(client_cert),
                client_private_key=str(client_key),
                server_certificate=str(server_cert),
                application_uri=client_uri,
            )
            client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False, security=config)
            await client.connect()
            try:
                value = await client.read(node_id)
                assert value.value == 42
                assert client.authentication_mode == "USERNAME_PASSWORD"
                assert client.security_summary == "Basic256Sha256/Sign"
            finally:
                await client.disconnect()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_x509_user_identity_is_separate_from_application_identity(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint("devagent/x509-user")
        server_uri = "urn:devagent:qualification:x509-server"
        app_uri = "urn:devagent:qualification:x509-app-client"
        user_uri = "urn:devagent:qualification:x509-user"

        server_key = tmp_path / "x509-server-key.pem"
        server_cert = tmp_path / "x509-server.der"
        app_key = tmp_path / "x509-app-key.pem"
        app_cert = tmp_path / "x509-app.der"
        user_key = tmp_path / "x509-user-key.pem"
        user_cert = tmp_path / "x509-user.der"
        _create_certificate(server_key, server_cert, server_uri, "X509 Server", server=True)
        _create_certificate(app_key, app_cert, app_uri, "DevAgent Application")
        _create_certificate(user_key, user_cert, user_uri, "DevAgent ReadOnly User")

        manager = CertificateUserManager()
        await manager.add_user(user_cert, name="devagent-reader")
        server, node_id = await _build_server(
            endpoint=endpoint,
            server_uri=server_uri,
            server_certificate=str(server_cert),
            server_private_key=str(server_key),
            security_policy_type=ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
            identity_token_type=ua.X509IdentityToken,
            user_manager=manager,
        )
        try:
            config = LiveSecurityConfig(
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                client_certificate=str(app_cert),
                client_private_key=str(app_key),
                server_certificate=str(server_cert),
                user_certificate=str(user_cert),
                user_private_key=str(user_key),
                application_uri=app_uri,
            )
            client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False, security=config)
            await client.connect()
            try:
                value = await client.read(node_id)
                assert value.value == 42
                assert client.authentication_mode == "X509_USER_CERTIFICATE"
                assert client.security_summary == "Basic256Sha256/SignAndEncrypt"
            finally:
                await client.disconnect()
        finally:
            await server.stop()

    asyncio.run(scenario())
