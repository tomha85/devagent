from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import LiveConfigurationError

MODERN_SECURITY_POLICIES = (
    "Basic256Sha256",
    "Aes128Sha256RsaOaep",
    "Aes256Sha256RsaPss",
)
DEPRECATED_SECURITY_POLICIES = (
    "Basic128Rsa15",
    "Basic256",
)
RUNTIME_SUPPORTED_SECURITY_POLICIES = (
    *DEPRECATED_SECURITY_POLICIES,
    *MODERN_SECURITY_POLICIES,
)
ECC_SECURITY_POLICIES = (
    "EccNistP256",
    "EccNistP384",
    "EccBrainpoolP256r1",
    "EccBrainpoolP384r1",
    "EccCurve25519",
)
STANDARD_SECURITY_POLICIES = (
    *RUNTIME_SUPPORTED_SECURITY_POLICIES,
    *ECC_SECURITY_POLICIES,
)

# Backward-compatible public name used by existing CLI/tests.
SUPPORTED_SECURITY_POLICIES = RUNTIME_SUPPORTED_SECURITY_POLICIES
SUPPORTED_SECURITY_MODES = ("Sign", "SignAndEncrypt")

_POLICY_ALIASES = {
    "basic128rsa15": "Basic128Rsa15",
    "basic256": "Basic256",
    "basic256sha256": "Basic256Sha256",
    "aes128sha256rsaoaep": "Aes128Sha256RsaOaep",
    "aes256sha256rsapss": "Aes256Sha256RsaPss",
    "eccnistp256": "EccNistP256",
    "eccnistp384": "EccNistP384",
    "eccbrainpoolp256r1": "EccBrainpoolP256r1",
    "eccbrainpoolp384r1": "EccBrainpoolP384r1",
    "ecccurve25519": "EccCurve25519",
}


def canonical_security_policy_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if "#" in text:
        text = text.rsplit("#", 1)[-1]
    elif "/" in text:
        text = text.rsplit("/", 1)[-1]
    key = "".join(ch for ch in text.lower() if ch.isalnum())
    return _POLICY_ALIASES.get(key, text)


def validate_opcua_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp":
        raise LiveConfigurationError("DevAgent Live requires an opc.tcp:// endpoint")
    if not parsed.hostname:
        raise LiveConfigurationError("OPC UA endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise LiveConfigurationError(
            "Credentials embedded in the OPC UA endpoint are not allowed; "
            "configure authentication separately"
        )
    return endpoint


@dataclass(frozen=True)
class LiveSecurityConfig:
    # Keep the original positional constructor order stable. New V1 profile
    # fields are appended after application_uri so existing positional callers
    # do not silently bind old arguments to new meanings.
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    security_policy: str | None = None
    security_mode: str | None = None
    client_certificate: str | None = None
    client_private_key: str | None = None
    private_key_password: str | None = field(default=None, repr=False)
    server_certificate: str | None = None
    application_uri: str = "urn:devagent:live:client"
    user_certificate: str | None = None
    user_private_key: str | None = None
    user_private_key_password: str | None = field(default=None, repr=False)
    allow_insecure_username_password: bool = False
    trust_store: str | None = None
    crl_store: str | None = None

    def __post_init__(self) -> None:
        canonical_policy = canonical_security_policy_name(self.security_policy)
        if canonical_policy != self.security_policy:
            object.__setattr__(self, "security_policy", canonical_policy)

        if self.username is not None and not self.username.strip():
            raise LiveConfigurationError("OPC UA username cannot be blank")
        if (self.username is None) != (self.password is None):
            raise LiveConfigurationError(
                "OPC UA username and password must be configured together"
            )
        if self.allow_insecure_username_password and self.username is None:
            raise LiveConfigurationError(
                "--allow-insecure-username-password requires OPC UA username/password configuration"
            )
        if (self.user_certificate is None) != (self.user_private_key is None):
            raise LiveConfigurationError(
                "OPC UA X.509 user certificate and user private key must be configured together"
            )
        if self.username is not None and self.user_certificate is not None:
            raise LiveConfigurationError(
                "Choose exactly one OPC UA user identity: username/password or X.509 user certificate"
            )
        if self.user_private_key_password is not None and self.user_private_key is None:
            raise LiveConfigurationError(
                "A user private-key password requires X.509 user-certificate configuration"
            )
        if not self.application_uri.strip():
            raise LiveConfigurationError("OPC UA application URI cannot be blank")
        if self.crl_store is not None and self.trust_store is None:
            raise LiveConfigurationError(
                "OPC UA --crl-store requires --trust-store"
            )

        secure_fields = (
            self.security_policy,
            self.security_mode,
            self.client_certificate,
            self.client_private_key,
            self.server_certificate,
            self.trust_store,
            self.crl_store,
        )
        secure_requested = any(value is not None for value in secure_fields)
        if not secure_requested:
            if self.private_key_password is not None:
                raise LiveConfigurationError(
                    "A private-key password requires secure-channel certificate configuration"
                )
            if self.username is not None and not self.allow_insecure_username_password:
                raise LiveConfigurationError(
                    "Username/password authentication over a NoSecurity channel is blocked by default; "
                    "use --allow-insecure-username-password only when the existing OPC UA server profile requires it"
                )
        else:
            if self.allow_insecure_username_password:
                raise LiveConfigurationError(
                    "--allow-insecure-username-password is only valid when no secure-channel policy is configured"
                )
            if self.security_policy in ECC_SECURITY_POLICIES:
                raise LiveConfigurationError(
                    f"OPC UA security policy {self.security_policy!r} is recognized by the standard "
                    "but is not available in the supported asyncua >=2,<3 runtime"
                )
            if self.security_policy not in RUNTIME_SUPPORTED_SECURITY_POLICIES:
                allowed = ", ".join(STANDARD_SECURITY_POLICIES)
                raise LiveConfigurationError(
                    f"Unsupported OPC UA security policy {self.security_policy!r}; recognized profiles: {allowed}"
                )
            if self.security_mode not in SUPPORTED_SECURITY_MODES:
                allowed = ", ".join(SUPPORTED_SECURITY_MODES)
                raise LiveConfigurationError(
                    f"Unsupported OPC UA security mode {self.security_mode!r}; choose one of: {allowed}"
                )
            if not self.client_certificate:
                raise LiveConfigurationError("Secure OPC UA requires a client application certificate")
            if not self.client_private_key:
                raise LiveConfigurationError("Secure OPC UA requires a client application private key")
            if not self.server_certificate and not self.trust_store:
                raise LiveConfigurationError(
                    "Secure OPC UA requires a pinned server certificate or a trust store"
                )

        # Normalize home-directory references once so validation and asyncua use
        # exactly the same certificate/key/trust-store paths.
        for attribute in (
            "client_certificate",
            "client_private_key",
            "server_certificate",
            "user_certificate",
            "user_private_key",
            "trust_store",
            "crl_store",
        ):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(self, attribute, str(Path(value).expanduser()))

    @property
    def secure_channel(self) -> bool:
        return self.security_policy is not None

    @property
    def deprecated_policy(self) -> bool:
        return self.security_policy in DEPRECATED_SECURITY_POLICIES

    @property
    def insecure_username_password(self) -> bool:
        return self.username is not None and not self.secure_channel

    @property
    def authentication_mode(self) -> str:
        if self.user_certificate is not None:
            return "X509_USER_CERTIFICATE"
        if self.username is not None:
            return "USERNAME_PASSWORD"
        return "ANONYMOUS"

    @property
    def server_trust_mode(self) -> str:
        if self.server_certificate and self.trust_store:
            return "PIN_AND_TRUST_STORE"
        if self.server_certificate:
            return "PINNED_CERTIFICATE"
        if self.trust_store:
            return "TRUST_STORE"
        return "NONE"

    @property
    def channel_summary(self) -> str:
        if not self.secure_channel:
            return "NONE"
        suffix = " [DEPRECATED]" if self.deprecated_policy else ""
        return f"{self.security_policy}/{self.security_mode}{suffix}"

    def validate_files(self) -> None:
        required_files: list[tuple[str, str | None]] = []
        if self.secure_channel:
            required_files.extend(
                [
                    ("client application certificate", self.client_certificate),
                    ("client application private key", self.client_private_key),
                ]
            )
            if self.server_certificate is not None:
                required_files.append(("server certificate", self.server_certificate))
        if self.user_certificate is not None:
            required_files.extend(
                [
                    ("X.509 user certificate", self.user_certificate),
                    ("X.509 user private key", self.user_private_key),
                ]
            )

        for label, value in required_files:
            assert value is not None
            path = Path(value)
            if not path.is_file():
                raise LiveConfigurationError(f"OPC UA {label} file does not exist: {path}")

        if self.trust_store is not None:
            path = Path(self.trust_store)
            if not path.is_dir():
                raise LiveConfigurationError(f"OPC UA trust store directory does not exist: {path}")
            trusted = [item for item in path.iterdir() if item.is_file() and item.suffix.lower() in {".der", ".pem"}]
            if not trusted:
                raise LiveConfigurationError(
                    f"OPC UA trust store contains no .der/.pem trusted certificates: {path}"
                )

        if self.crl_store is not None:
            path = Path(self.crl_store)
            if not path.is_dir():
                raise LiveConfigurationError(f"OPC UA CRL store directory does not exist: {path}")
            crls = [item for item in path.iterdir() if item.is_file() and item.suffix.lower() in {".der", ".pem"}]
            if not crls:
                raise LiveConfigurationError(
                    f"OPC UA CRL store contains no .der/.pem revocation lists: {path}"
                )

    def redact(self, text: str) -> str:
        redacted = text
        for secret in (
            self.password,
            self.private_key_password,
            self.user_private_key_password,
        ):
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return redacted
