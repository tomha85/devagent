from __future__ import annotations

import argparse

import pytest

from devagent.live.cli_security import add_security_args, security_from_args
from devagent.live.connection_guidance import analyze_connection_guidance, format_connection_guidance
from devagent.live.errors import LiveConfigurationError
from devagent.live.models import EndpointSummary
from devagent.live.security import LiveSecurityConfig


def test_no_security_username_is_blocked_by_default() -> None:
    with pytest.raises(LiveConfigurationError, match="blocked by default"):
        LiveSecurityConfig(username="operator", password="secret")


def test_no_security_username_can_be_explicitly_enabled() -> None:
    config = LiveSecurityConfig(
        username="operator",
        password="secret",
        allow_insecure_username_password=True,
    )
    assert config.authentication_mode == "USERNAME_PASSWORD"
    assert config.channel_summary == "NONE"
    assert config.insecure_username_password is True
    assert "secret" not in repr(config)


def test_insecure_opt_in_without_username_is_rejected() -> None:
    with pytest.raises(LiveConfigurationError, match="requires OPC UA username/password"):
        LiveSecurityConfig(allow_insecure_username_password=True)


def test_insecure_opt_in_is_rejected_when_secure_channel_is_configured(tmp_path) -> None:
    client_cert = tmp_path / "client.der"
    client_key = tmp_path / "client.pem"
    server_cert = tmp_path / "server.der"
    for path in (client_cert, client_key, server_cert):
        path.write_text("test", encoding="utf-8")

    with pytest.raises(LiveConfigurationError, match="only valid when no secure-channel"):
        LiveSecurityConfig(
            username="operator",
            password="secret",
            allow_insecure_username_password=True,
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate=str(client_cert),
            client_private_key=str(client_key),
            server_certificate=str(server_cert),
        )


def test_cli_requires_explicit_insecure_username_flag(monkeypatch) -> None:
    monkeypatch.setenv("PLC_PASSWORD", "secret")
    parser = argparse.ArgumentParser()
    add_security_args(parser)

    blocked = parser.parse_args(
        ["--username", "operator", "--password-env", "PLC_PASSWORD"]
    )
    with pytest.raises(LiveConfigurationError, match="blocked by default"):
        security_from_args(blocked)

    opted_in = parser.parse_args(
        [
            "--username",
            "operator",
            "--password-env",
            "PLC_PASSWORD",
            "--allow-insecure-username-password",
        ]
    )
    config = security_from_args(opted_in)
    assert config.allow_insecure_username_password is True
    assert config.insecure_username_password is True


def test_probe_guidance_reports_insecure_username_profile_without_recommending_it() -> None:
    endpoint = EndpointSummary(
        endpoint_url="opc.tcp://192.168.10.20:4840/",
        security_mode="None_",
        security_policy_uri="http://opcfoundation.org/UA/SecurityPolicy#None",
        user_token_types=("UserName",),
        server_application_name="Legacy PLC OPC UA Server",
    )
    guidance = analyze_connection_guidance([endpoint])
    assessment = guidance.assessments[0]

    assert assessment.supported is False
    assert assessment.support_status == "BLOCKED_BY_POLICY"
    assert assessment.username_password_available is True
    assert assessment.username_password_requires_insecure_opt_in is True
    assert guidance.recommended is None
    assert guidance.insecure_username_password_advertised is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "explicit insecure opt-in required" in rendered
    assert "--allow-insecure-username-password" in rendered
    assert "never enable or select" in rendered.lower()
