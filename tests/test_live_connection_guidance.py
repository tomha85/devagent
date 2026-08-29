from __future__ import annotations

from devagent.live.connection_guidance import (
    analyze_connection_guidance,
    assess_endpoint,
    format_connection_guidance,
)
from devagent.live.models import EndpointSummary


def _endpoint(
    *,
    mode: str,
    policy: str,
    tokens: tuple[str, ...],
    url: str = "opc.tcp://192.168.10.20:4840/",
) -> EndpointSummary:
    policy_uri = (
        "http://opcfoundation.org/UA/SecurityPolicy#None"
        if policy == "None"
        else f"http://opcfoundation.org/UA/SecurityPolicy#{policy}"
    )
    return EndpointSummary(
        endpoint_url=url,
        security_mode=mode,
        security_policy_uri=policy_uri,
        user_token_types=tokens,
        server_application_name="Test PLC OPC UA Server",
    )


def test_anonymous_no_security_does_not_require_certificate() -> None:
    guidance = analyze_connection_guidance(
        [_endpoint(mode="None_", policy="None", tokens=("Anonymous",))]
    )

    assert guidance.certificate_required_to_connect is False
    assert guidance.certificate_free_connection_available is True
    assert guidance.secure_connection_available is False
    assert guidance.anonymous_available is True
    assert guidance.username_password_available is False
    assert guidance.recommended is not None
    assert guidance.recommended.certificate_required is False

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: NO" in rendered
    assert "Client certificate: NOT REQUIRED" in rendered


def test_mixed_server_reports_certificate_not_required_but_recommends_secure_profile() -> None:
    insecure = _endpoint(mode="None_", policy="None", tokens=("Anonymous",))
    secure = _endpoint(
        mode="SignAndEncrypt",
        policy="Aes256Sha256RsaPss",
        tokens=("Anonymous", "UserName"),
        url="opc.tcp://192.168.10.20:4840/secure",
    )

    guidance = analyze_connection_guidance([insecure, secure])

    assert guidance.certificate_required_to_connect is False
    assert guidance.certificate_free_connection_available is True
    assert guidance.secure_connection_available is True
    assert guidance.username_password_available is True
    assert guidance.recommended is not None
    assert guidance.recommended.endpoint.endpoint_url.endswith("/secure")
    assert guidance.recommended.policy_name == "Aes256Sha256RsaPss"
    assert guidance.recommended.certificate_required is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: NO" in rendered
    assert "Recommended production profile:" in rendered
    assert "Client certificate: REQUIRED" in rendered


def test_secure_username_only_profile_requires_certificate_and_signandencrypt() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic256Sha256",
        tokens=("UserName",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is True
    assert assessment.secure_channel is True
    assert assessment.certificate_required is True
    assert assessment.username_password_available is True
    assert assessment.anonymous_available is False
    assert guidance.certificate_required_to_connect is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: YES" in rendered
    assert "Username/password available: YES" in rendered
    assert "Password: provide through --password-env" in rendered


def test_username_over_sign_only_is_not_recommended() -> None:
    endpoint = _endpoint(
        mode="Sign",
        policy="Basic256Sha256",
        tokens=("UserName",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is False
    assert "requires SignAndEncrypt" in assessment.reason
    assert guidance.recommended is None
    assert guidance.certificate_required_to_connect is None


def test_unsupported_security_policy_fails_closed() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic128Rsa15",
        tokens=("Anonymous",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is False
    assert "not in DevAgent Live's supported policy set" in assessment.reason
    assert guidance.recommended is None

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: UNKNOWN" in rendered
    assert "Recommended profile: NONE" in rendered
