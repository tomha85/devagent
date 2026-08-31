from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from .errors import LiveConfigurationError

CERTIFICATE_SUFFIXES = frozenset({".der", ".pem", ".cer", ".crt", ".pfx", ".p12"})
PKCS12_SUFFIXES = frozenset({".pfx", ".p12"})
CRL_SUFFIXES = frozenset({".der", ".pem", ".crl"})


@dataclass(frozen=True)
class Pkcs12Bundle:
    private_key: Any | None
    certificate: x509.Certificate | None
    additional_certificates: tuple[x509.Certificate, ...]


def is_pkcs12_path(path: str | Path | None) -> bool:
    if path is None:
        return False
    return Path(path).suffix.lower() in PKCS12_SUFFIXES


def _password_bytes(password: str | bytes | None) -> bytes | None:
    if isinstance(password, str):
        return password.encode("utf-8")
    return password


def _read(path: str | Path, *, label: str) -> bytes:
    source = Path(path)
    try:
        return source.read_bytes()
    except OSError as exc:
        raise LiveConfigurationError(f"Unable to read OPC UA {label}: {source}: {exc}") from None


def _looks_pem(content: bytes, marker: bytes) -> bool:
    return marker in content[:4096]


def certificate_encoding(path: str | Path) -> str:
    """Return the actual DER/PEM encoding for a single-certificate file.

    `.cer` and `.crt` are container/file-name conventions rather than encodings,
    so DevAgent detects PEM from the content and otherwise treats the input as DER.
    """

    content = _read(path, label="certificate")
    return "pem" if _looks_pem(content, b"-----BEGIN CERTIFICATE-----") else "der"


def private_key_encoding(path: str | Path) -> str:
    content = _read(path, label="private key")
    return "pem" if b"-----BEGIN" in content[:4096] and b"PRIVATE KEY-----" in content[:4096] else "der"


def load_pkcs12_bundle(
    path: str | Path,
    *,
    password: str | bytes | None,
    label: str,
) -> Pkcs12Bundle:
    source = Path(path)
    content = _read(source, label=label)
    try:
        private_key, certificate, additional = pkcs12.load_key_and_certificates(
            content,
            _password_bytes(password),
        )
    except (TypeError, ValueError):
        raise LiveConfigurationError(
            f"Unable to load OPC UA {label} PKCS#12 bundle {source}; verify the .pfx/.p12 file and password"
        ) from None
    return Pkcs12Bundle(
        private_key=private_key,
        certificate=certificate,
        additional_certificates=tuple(additional or ()),
    )


def certificate_der(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.DER)


def private_key_der(private_key: Any) -> bytes:
    try:
        return private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    except (TypeError, ValueError, AttributeError):
        raise LiveConfigurationError("OPC UA PKCS#12 bundle does not contain a usable private key") from None


def load_certificate_objects(
    path: str | Path,
    *,
    password: str | bytes | None = None,
    label: str = "certificate",
) -> tuple[x509.Certificate, ...]:
    source = Path(path)
    if is_pkcs12_path(source):
        bundle = load_pkcs12_bundle(source, password=password, label=label)
        certificates: list[x509.Certificate] = []
        if bundle.certificate is not None:
            certificates.append(bundle.certificate)
        certificates.extend(bundle.additional_certificates)
        if not certificates:
            raise LiveConfigurationError(
                f"OPC UA {label} PKCS#12 bundle contains no certificates: {source}"
            )
        return tuple(certificates)

    content = _read(source, label=label)
    try:
        if _looks_pem(content, b"-----BEGIN CERTIFICATE-----"):
            loader = getattr(x509, "load_pem_x509_certificates", None)
            if callable(loader):
                certificates = tuple(loader(content))
            else:  # pragma: no cover - cryptography>=42 provides the multi-cert loader
                certificates = (x509.load_pem_x509_certificate(content),)
        else:
            certificates = (x509.load_der_x509_certificate(content),)
    except ValueError:
        raise LiveConfigurationError(
            f"OPC UA {label} is not a valid X.509 DER/PEM certificate: {source}"
        ) from None
    if not certificates:
        raise LiveConfigurationError(f"OPC UA {label} contains no certificates: {source}")
    return certificates


def load_crl_object(path: str | Path, *, label: str = "CRL") -> x509.CertificateRevocationList:
    source = Path(path)
    content = _read(source, label=label)
    try:
        if _looks_pem(content, b"-----BEGIN X509 CRL-----"):
            return x509.load_pem_x509_crl(content)
        return x509.load_der_x509_crl(content)
    except ValueError:
        raise LiveConfigurationError(
            f"OPC UA {label} is not a valid DER/PEM certificate revocation list: {source}"
        ) from None


def trust_store_files(directory: str | Path) -> tuple[Path, ...]:
    root = Path(directory)
    return tuple(
        sorted(
            item
            for item in root.iterdir()
            if item.is_file() and item.suffix.lower() in CERTIFICATE_SUFFIXES
        )
    )


def crl_store_files(directory: str | Path) -> tuple[Path, ...]:
    root = Path(directory)
    return tuple(
        sorted(
            item
            for item in root.iterdir()
            if item.is_file() and item.suffix.lower() in CRL_SUFFIXES
        )
    )
