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
    assert guidance.recommended.application_certificate_required is False
    assert guidance.recommended.user_certificate_required is False

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: NO" in rendered
    assert "Client application certificate: NOT REQUIRED" in rendered


def test_no_security_x509_only_requires_user_certificate_not_application_certificate() -> None:
    guidance = analyze_connection_guidance(
        [_endpoint(mode="None_", policy="None", tokens=("Certificate",))]
    )

    assert guidance.certificate_required_to_connect is True
    assert guidance.certificate_free_connection_available is False
    assert guidance.secure_connection_available is False
    assert guidance.user_certificate_available is True
    assert guidance.recommended is not None
    assert guidance.recommended.certificate_required is True
    assert guidance.recommended.application_certificate_required is False
    assert guidance.recommended.user_certificate_required is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: YES" in rendered
    assert "Client application certificate: NOT REQUIRED" in rendered
    assert "X.509 user identity: REQUIRED" in rendered


def test_no_security_anonymous_plus_x509_has_certificate_free_path() -> None:
    guidance = analyze_connection_guidance(
        [_endpoint(mode="None_", policy="None", tokens=("Anonymous", "Certificate"))]
    )

    assert guidance.certificate_required_to_connect is False
    assert guidance.certificate_free_connection_available is True
    assert guidance.recommended is not None
    assert guidance.recommended.user_certificate_available is True
    assert guidance.recommended.user_certificate_required is False


def test_mixed_server_reports_certificate_not_required_but_recommends_secure_profile() -> None:
    insecure = _endpoint(mode="None_", policy="None", tokens=("Anonymous",))
    secure = _endpoint(
        mode="SignAndEncrypt",
        policy="Aes256_Sha256_RsaPss",
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
    assert guidance.recommended.application_certificate_required is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: NO" in rendered
    assert "Recommended production profile:" in rendered
    assert "Client application certificate: REQUIRED" in rendered


def test_secure_username_signandencrypt_profile_is_supported() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic256Sha256",
        tokens=("UserName",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is True
    assert assessment.support_status == "SUPPORTED"
    assert assessment.secure_channel is True
    assert assessment.certificate_required is True
    assert assessment.application_certificate_required is True
    assert assessment.username_password_available is True
    assert assessment.anonymous_available is False
    assert guidance.certificate_required_to_connect is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "Certificate required to connect: YES" in rendered
    assert "Username/password available: YES" in rendered
    assert "Password: provide through --password-env" in rendered


def test_username_over_sign_is_supported_on_secure_channel() -> None:
    endpoint = _endpoint(
        mode="Sign",
        policy="Basic256Sha256",
        tokens=("UserName",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is True
    assert assessment.username_password_available is True
    assert assessment.mode_name == "Sign"
    assert assessment.application_certificate_required is True
    assert guidance.recommended is not None


def test_username_over_no_security_is_blocked_by_policy() -> None:
    endpoint = _endpoint(mode="None_", policy="None", tokens=("UserName",))
    assessment = assess_endpoint(endpoint)

    assert assessment.supported is False
    assert assessment.support_status == "BLOCKED_BY_POLICY"
    assert "NoSecurity" in assessment.reason


def test_x509_user_certificate_profile_is_supported() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic256Sha256",
        tokens=("Certificate",),
    )
    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is True
    assert assessment.user_certificate_available is True
    assert assessment.user_certificate_required is True
    assert assessment.application_certificate_required is True
    assert guidance.user_certificate_available is True
    rendered = "\n".join(format_connection_guidance(guidance))
    assert "X.509 user-certificate authentication available: YES" in rendered
    assert "X.509 user identity: REQUIRED" in rendered
    assert "--user-certificate" in rendered


def test_legacy_policy_is_deprecated_compatibility_not_unsupported() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic128Rsa15",
        tokens=("Anonymous",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is True
    assert assessment.deprecated_policy is True
    assert assessment.support_status == "DEPRECATED_COMPATIBILITY"
    assert guidance.recommended is not None

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "deprecated by OPC UA" in rendered


def test_ecc_profile_is_recognized_as_runtime_unavailable() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="ECC_nistP256",
        tokens=("Anonymous",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is False
    assert assessment.support_status == "RUNTIME_UNAVAILABLE"
    assert "ECC" in assessment.reason
    assert guidance.recommended is None


def test_issued_token_only_profile_is_recognized_as_runtime_unavailable() -> None:
    endpoint = _endpoint(
        mode="SignAndEncrypt",
        policy="Basic256Sha256",
        tokens=("IssuedToken",),
    )

    assessment = assess_endpoint(endpoint)
    guidance = analyze_connection_guidance([endpoint])

    assert assessment.supported is False
    assert assessment.issued_token_available is True
    assert assessment.support_status == "RUNTIME_UNAVAILABLE"
    assert guidance.issued_token_advertised is True

    rendered = "\n".join(format_connection_guidance(guidance))
    assert "IssuedToken/JWT advertised: YES (runtime unavailable)" in rendered
