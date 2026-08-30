from __future__ import annotations

import argparse
import os

from .errors import LiveConfigurationError
from .security import (
    STANDARD_SECURITY_POLICIES,
    SUPPORTED_SECURITY_MODES,
    LiveSecurityConfig,
)


def add_security_args(parser: argparse.ArgumentParser) -> None:
    # Security-related long options must never be prefix-matched. Without this,
    # argparse can interpret a literal --password flag as --password-env.
    parser.allow_abbrev = False
    group = parser.add_argument_group("OPC UA security")
    group.add_argument(
        "--username",
        help="OPC UA username. Password must be supplied through --password-env.",
    )
    group.add_argument(
        "--password-env",
        metavar="ENV_VAR",
        help="Environment variable containing the OPC UA password; the password is never accepted on argv.",
    )
    group.add_argument(
        "--security-policy",
        metavar="POLICY",
        help=(
            "OPC UA secure-channel policy. Canonical names and standard URI-fragment aliases are accepted. "
            "Recognized profiles: " + ", ".join(STANDARD_SECURITY_POLICIES) + ". "
            "Basic128Rsa15/Basic256 are deprecated compatibility profiles; ECC profiles are recognized "
            "but fail closed when the installed asyncua runtime cannot implement them."
        ),
    )
    group.add_argument(
        "--security-mode",
        choices=SUPPORTED_SECURITY_MODES,
        help="OPC UA secure-channel mode.",
    )
    group.add_argument(
        "--client-certificate",
        help="Path to the client application certificate (.der or .pem) used for the secure channel.",
    )
    group.add_argument(
        "--client-private-key",
        help="Path to the client application private key (.pem or .der) used for the secure channel.",
    )
    group.add_argument(
        "--private-key-password-env",
        metavar="ENV_VAR",
        help="Environment variable containing the client application private-key password, when encrypted.",
    )
    group.add_argument(
        "--server-certificate",
        help="Path to the pinned OPC UA server certificate. Required for secure channels.",
    )
    group.add_argument(
        "--user-certificate",
        help="Path to an X.509 user-identity certificate when the server authenticates users by certificate.",
    )
    group.add_argument(
        "--user-private-key",
        help="Path to the private key associated with --user-certificate.",
    )
    group.add_argument(
        "--user-private-key-password-env",
        metavar="ENV_VAR",
        help="Environment variable containing the X.509 user private-key password, when encrypted.",
    )
    group.add_argument(
        "--application-uri",
        default="urn:devagent:live:client",
        help="Client OPC UA application URI embedded in/associated with the client application certificate.",
    )


def _secret_from_env(variable_name: str | None, *, label: str) -> str | None:
    if variable_name is None:
        return None
    if not variable_name.strip():
        raise LiveConfigurationError(f"{label} environment-variable name cannot be blank")
    if variable_name not in os.environ:
        raise LiveConfigurationError(
            f"{label} environment variable {variable_name!r} is not set"
        )
    return os.environ[variable_name]


def security_from_args(args: argparse.Namespace) -> LiveSecurityConfig:
    password = _secret_from_env(
        getattr(args, "password_env", None),
        label="OPC UA password",
    )
    private_key_password = _secret_from_env(
        getattr(args, "private_key_password_env", None),
        label="OPC UA private-key password",
    )
    user_private_key_password = _secret_from_env(
        getattr(args, "user_private_key_password_env", None),
        label="OPC UA user private-key password",
    )
    return LiveSecurityConfig(
        username=getattr(args, "username", None),
        password=password,
        security_policy=getattr(args, "security_policy", None),
        security_mode=getattr(args, "security_mode", None),
        client_certificate=getattr(args, "client_certificate", None),
        client_private_key=getattr(args, "client_private_key", None),
        private_key_password=private_key_password,
        server_certificate=getattr(args, "server_certificate", None),
        user_certificate=getattr(args, "user_certificate", None),
        user_private_key=getattr(args, "user_private_key", None),
        user_private_key_password=user_private_key_password,
        application_uri=getattr(args, "application_uri", "urn:devagent:live:client"),
    )
