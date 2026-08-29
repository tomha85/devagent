from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import LiveConfigurationError

SUPPORTED_SECURITY_POLICIES = (
    "Basic256Sha256",
    "Aes128Sha256RsaOaep",
    "Aes256Sha256RsaPss",
)
SUPPORTED_SECURITY_MODES = ("Sign", "SignAndEncrypt")


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
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    security_policy: str | None = None
    security_mode: str | None = None
    client_certificate: str | None = None
    client_private_key: str | None = None
    private_key_password: str | None = field(default=None, repr=False)
    server_certificate: str | None = None
    application_uri: str = "urn:devagent:live:client"

    def __post_init__(self) -> None:
        if self.username is not None and not self.username.strip():
            raise LiveConfigurationError("OPC UA username cannot be blank")
        if (self.username is None) != (self.password is None):
            raise LiveConfigurationError(
                "OPC UA username and password must be configured together"
            )
        if not self.application_uri.strip():
            raise LiveConfigurationError("OPC UA application URI cannot be blank")

        secure_fields = (
            self.security_policy,
            self.security_mode,
            self.client_certificate,
            self.client_private_key,
            self.server_certificate,
        )
        secure_requested = any(value is not None for value in secure_fields)
        if not secure_requested:
            if self.private_key_password is not None:
                raise LiveConfigurationError(
                    "A private-key password requires secure-channel certificate configuration"
                )
            if self.username is not None:
                raise LiveConfigurationError(
                    "Username/password authentication requires an OPC UA SignAndEncrypt channel"
                )
            return

        if self.security_policy not in SUPPORTED_SECURITY_POLICIES:
            allowed = ", ".join(SUPPORTED_SECURITY_POLICIES)
            raise LiveConfigurationError(
                f"Unsupported OPC UA security policy {self.security_policy!r}; choose one of: {allowed}"
            )
        if self.security_mode not in SUPPORTED_SECURITY_MODES:
            allowed = ", ".join(SUPPORTED_SECURITY_MODES)
            raise LiveConfigurationError(
                f"Unsupported OPC UA security mode {self.security_mode!r}; choose one of: {allowed}"
            )
        if not self.client_certificate:
            raise LiveConfigurationError("Secure OPC UA requires a client certificate")
        if not self.client_private_key:
            raise LiveConfigurationError("Secure OPC UA requires a client private key")
        if not self.server_certificate:
            raise LiveConfigurationError(
                "Secure OPC UA requires a pinned server certificate"
            )
        if self.username is not None and self.security_mode != "SignAndEncrypt":
            raise LiveConfigurationError(
                "Username/password authentication requires security mode SignAndEncrypt"
            )

        # Normalize home-directory references once so validation and the
        # asyncua security loader use exactly the same certificate/key paths.
        for attribute in (
            "client_certificate",
            "client_private_key",
            "server_certificate",
        ):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(self, attribute, str(Path(value).expanduser()))

    @property
    def secure_channel(self) -> bool:
        return self.security_policy is not None

    @property
    def authentication_mode(self) -> str:
        return "USERNAME_PASSWORD" if self.username is not None else "ANONYMOUS"

    @property
    def channel_summary(self) -> str:
        if not self.secure_channel:
            return "NONE"
        return f"{self.security_policy}/{self.security_mode}"

    def validate_files(self) -> None:
        if not self.secure_channel:
            return
        for label, value in (
            ("client certificate", self.client_certificate),
            ("client private key", self.client_private_key),
            ("server certificate", self.server_certificate),
        ):
            assert value is not None
            path = Path(value)
            if not path.is_file():
                raise LiveConfigurationError(f"OPC UA {label} file does not exist: {path}")

    def redact(self, text: str) -> str:
        redacted = text
        for secret in (self.password, self.private_key_password):
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return redacted
