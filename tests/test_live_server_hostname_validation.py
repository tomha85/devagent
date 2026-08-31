from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from devagent.live.errors import LiveConnectionError
from devagent.live.opcua_client import _validate_server_certificate_hostname


def _certificate(*, dns_names: tuple[str, ...] = (), ip_addresses: tuple[str, ...] = ()) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test OPC UA Server")])
    san_values: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    san_values.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
    )
    if san_values:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_values), critical=False)
    return builder.sign(key, hashes.SHA256())


def test_server_hostname_accepts_matching_dns_san() -> None:
    certificate = _certificate(dns_names=("plc01.factory.local",))
    _validate_server_certificate_hostname(
        certificate,
        "opc.tcp://plc01.factory.local:4840/",
    )


def test_server_hostname_accepts_matching_ip_san() -> None:
    certificate = _certificate(ip_addresses=("10.20.30.40",))
    _validate_server_certificate_hostname(
        certificate,
        "opc.tcp://10.20.30.40:4840/",
    )


def test_server_hostname_rejects_wrong_server_from_same_ca_domain() -> None:
    certificate = _certificate(dns_names=("plc02.factory.local",))
    with pytest.raises(LiveConnectionError, match="does not match endpoint host"):
        _validate_server_certificate_hostname(
            certificate,
            "opc.tcp://plc01.factory.local:4840/",
        )


def test_server_hostname_rejects_certificate_without_dns_or_ip_san() -> None:
    certificate = _certificate()
    with pytest.raises(LiveConnectionError, match="no SubjectAlternativeName"):
        _validate_server_certificate_hostname(
            certificate,
            "opc.tcp://plc01.factory.local:4840/",
        )
