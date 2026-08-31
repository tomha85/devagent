from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import EndpointSummary
from .security import (
    DEPRECATED_SECURITY_POLICIES,
    ECC_SECURITY_POLICIES,
    RUNTIME_SUPPORTED_SECURITY_POLICIES,
    SUPPORTED_SECURITY_MODES,
    canonical_security_policy_name,
)


def _normalized(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def security_policy_name(uri: str) -> str:
    canonical = canonical_security_policy_name(uri)
    return canonical or "None"


def _token_set(endpoint: EndpointSummary) -> set[str]:
    return {_normalized(token) for token in endpoint.user_token_types}


@dataclass(frozen=True)
class EndpointConnectionAssessment:
    endpoint: EndpointSummary
    supported: bool
    support_status: str
    secure_channel: bool
    # Legacy per-endpoint field consumed by `devagent live probe`. It now means
    # client *application* certificate requirement; X.509 user-certificate
    # requirements are represented separately by user_certificate_required.
    certificate_required: bool
    application_certificate_required: bool
    user_certificate_required: bool
    anonymous_available: bool
    username_password_available: bool
    username_password_requires_insecure_opt_in: bool
    user_certificate_available: bool
    issued_token_available: bool
    policy_name: str
    mode_name: str
    deprecated_policy: bool
    reason: str

    @property
    def authentication_summary(self) -> str:
        options: list[str] = []
        if self.user_certificate_available:
            options.append("X509_USER_CERTIFICATE")
        if self.username_password_available and not self.username_password_requires_insecure_opt_in:
            options.append("USERNAME_PASSWORD")
        elif (
            self.username_password_requires_insecure_opt_in
            and not self.anonymous_available
            and not self.user_certificate_available
        ):
            options.append("USERNAME_PASSWORD[EXPLICIT_INSECURE_OPT_IN]")
        if self.anonymous_available:
            options.append("ANONYMOUS")
        if self.issued_token_available:
            options.append("ISSUED_TOKEN[RUNTIME_UNAVAILABLE]")
        return " / ".join(options) if options else "UNSUPPORTED_OR_UNKNOWN"


@dataclass(frozen=True)
class OpcUaConnectionGuidance:
    assessments: tuple[EndpointConnectionAssessment, ...]
    recommended: EndpointConnectionAssessment | None

    @property
    def supported_assessments(self) -> tuple[EndpointConnectionAssessment, ...]:
        return tuple(item for item in self.assessments if item.supported)

    @property
    def certificate_free_connection_available(self) -> bool:
        return any(
            item.supported
            and not item.application_certificate_required
            and not item.user_certificate_required
            for item in self.assessments
        )

    @property
    def secure_connection_available(self) -> bool:
        return any(item.supported and item.secure_channel for item in self.assessments)

    @property
    def username_password_available(self) -> bool:
        return any(
            item.supported
            and item.username_password_available
            and not item.username_password_requires_insecure_opt_in
            for item in self.assessments
        )

    @property
    def insecure_username_password_advertised(self) -> bool:
        return any(item.username_password_requires_insecure_opt_in for item in self.assessments)

    @property
    def user_certificate_available(self) -> bool:
        return any(item.supported and item.user_certificate_available for item in self.assessments)

    @property
    def issued_token_advertised(self) -> bool:
        return any(item.issued_token_available for item in self.assessments)

    @property
    def anonymous_available(self) -> bool:
        return any(item.supported and item.anonymous_available for item in self.assessments)

    @property
    def certificate_required_to_connect(self) -> bool | None:
        supported = self.supported_assessments
        if not supported:
            return None
        return all(
            item.application_certificate_required or item.user_certificate_required
            for item in supported
        )


def assess_endpoint(endpoint: EndpointSummary) -> EndpointConnectionAssessment:
    mode_raw = (endpoint.security_mode or "").strip()
    mode_normalized = _normalized(mode_raw)
    mode_is_none = mode_normalized in {"", "none", "none0"}
    mode_name = "None" if mode_is_none else mode_raw
    policy_name = security_policy_name(endpoint.security_policy_uri)
    policy_is_none = _normalized(policy_name) in {"", "none"}
    tokens = _token_set(endpoint)

    anonymous = "anonymous" in tokens
    username = "username" in tokens or "usernametoken" in tokens
    user_certificate = (
        "certificate" in tokens
        or "x509" in tokens
        or "x509identitytoken" in tokens
    )
    issued_token = (
        "issuedtoken" in tokens
        or "issuedidentitytoken" in tokens
        or "jwt" in tokens
    )

    if mode_is_none and policy_is_none:
        direct_supported_identity = anonymous or user_certificate
        x509_required = user_certificate and not anonymous

        if direct_supported_identity:
            status = "SUPPORTED"
            if username:
                reason = (
                    "NoSecurity endpoint has a directly supported Anonymous/X.509 identity and also advertises "
                    "username/password, which requires explicit --allow-insecure-username-password opt-in when selected."
                )
            else:
                reason = "NoSecurity endpoint is usable with an advertised supported identity."
        elif username:
            status = "BLOCKED_BY_POLICY"
            reason = (
                "Endpoint advertises username/password over NoSecurity. DevAgent blocks this profile by default; "
                "the operator may use the existing customer/server profile only by explicitly supplying "
                "--allow-insecure-username-password. It is never enabled or selected silently."
            )
        elif issued_token:
            status = "RUNTIME_UNAVAILABLE"
            reason = (
                "Endpoint requires IssuedToken/JWT identity, which asyncua >=2,<3 "
                "does not expose as a DevAgent client login path."
            )
        else:
            status = "UNSUPPORTED"
            reason = "NoSecurity endpoint does not advertise a currently supported user identity."

        return EndpointConnectionAssessment(
            endpoint=endpoint,
            supported=direct_supported_identity,
            support_status=status,
            secure_channel=False,
            certificate_required=False,
            application_certificate_required=False,
            user_certificate_required=x509_required,
            anonymous_available=anonymous,
            username_password_available=username,
            username_password_requires_insecure_opt_in=username,
            user_certificate_available=user_certificate,
            issued_token_available=issued_token,
            policy_name="None",
            mode_name="None",
            deprecated_policy=False,
            reason=reason,
        )

    if mode_is_none != policy_is_none:
        return EndpointConnectionAssessment(
            endpoint=endpoint,
            supported=False,
            support_status="UNSUPPORTED",
            secure_channel=not mode_is_none,
            certificate_required=not mode_is_none,
            application_certificate_required=not mode_is_none,
            user_certificate_required=False,
            anonymous_available=anonymous,
            username_password_available=False,
            username_password_requires_insecure_opt_in=False,
            user_certificate_available=user_certificate,
            issued_token_available=issued_token,
            policy_name=policy_name,
            mode_name=mode_name or "-",
            deprecated_policy=False,
            reason=(
                "Endpoint advertises inconsistent security mode/policy metadata; "
                "refusing to infer a connection profile."
            ),
        )

    deprecated = policy_name in DEPRECATED_SECURITY_POLICIES
    runtime_policy_supported = policy_name in RUNTIME_SUPPORTED_SECURITY_POLICIES
    mode_supported = mode_raw in SUPPORTED_SECURITY_MODES
    username_supported = username and mode_supported
    x509_supported = user_certificate and mode_supported
    authentication_supported = anonymous or username_supported or x509_supported
    x509_required = x509_supported and not anonymous and not username_supported

    if policy_name in ECC_SECURITY_POLICIES:
        status = "RUNTIME_UNAVAILABLE"
        supported = False
        reason = (
            f"Security policy {policy_name!r} is a recognized OPC UA ECC profile, "
            "but asyncua >=2,<3 does not provide the required secure-channel policy implementation."
        )
    elif not runtime_policy_supported:
        status = "UNSUPPORTED"
        supported = False
        reason = f"Security policy {policy_name!r} is not in DevAgent Live's runtime-supported policy set."
    elif not mode_supported:
        status = "UNSUPPORTED"
        supported = False
        reason = f"Security mode {mode_raw or '-'} is not supported by DevAgent Live."
    elif not authentication_supported:
        status = "RUNTIME_UNAVAILABLE" if issued_token else "UNSUPPORTED"
        supported = False
        reason = (
            "Endpoint only advertises IssuedToken/JWT identity, which the supported asyncua runtime "
            "does not expose as a DevAgent client login path."
            if issued_token
            else (
                "Endpoint does not advertise a currently supported Anonymous, UserName, "
                "or X.509 Certificate identity token."
            )
        )
    else:
        supported = True
        status = "DEPRECATED_COMPATIBILITY" if deprecated else "SUPPORTED"
        reason = (
            "Supported deprecated OPC UA compatibility profile; use only when required by an older server."
            if deprecated
            else "Supported secure OPC UA profile; client application certificate material is required."
        )

    return EndpointConnectionAssessment(
        endpoint=endpoint,
        supported=supported,
        support_status=status,
        secure_channel=True,
        certificate_required=True,
        application_certificate_required=True,
        user_certificate_required=x509_required,
        anonymous_available=anonymous,
        username_password_available=username_supported,
        username_password_requires_insecure_opt_in=False,
        user_certificate_available=x509_supported,
        issued_token_available=issued_token,
        policy_name=policy_name,
        mode_name=mode_raw or "-",
        deprecated_policy=deprecated,
        reason=reason,
    )


def _recommendation_rank(item: EndpointConnectionAssessment) -> tuple[int, int, int, int, int, int]:
    policy_rank = {
        "Basic128Rsa15": -2,
        "Basic256": -1,
        "Basic256Sha256": 1,
        "Aes128Sha256RsaOaep": 2,
        "Aes256Sha256RsaPss": 3,
    }.get(item.policy_name, 0)
    mode_rank = {"Sign": 1, "SignAndEncrypt": 2}.get(item.mode_name, 0)
    return (
        1 if not item.username_password_requires_insecure_opt_in else 0,
        1 if not item.deprecated_policy else 0,
        1 if item.secure_channel else 0,
        mode_rank,
        policy_rank,
        1 if item.user_certificate_available or item.username_password_available else 0,
    )


def analyze_connection_guidance(
    endpoints: Iterable[EndpointSummary],
) -> OpcUaConnectionGuidance:
    assessments = tuple(assess_endpoint(endpoint) for endpoint in endpoints)
    supported = [item for item in assessments if item.supported]
    recommended = max(supported, key=_recommendation_rank) if supported else None
    return OpcUaConnectionGuidance(assessments=assessments, recommended=recommended)


def format_connection_guidance(guidance: OpcUaConnectionGuidance) -> list[str]:
    required = guidance.certificate_required_to_connect
    if required is None:
        certificate_text = "UNKNOWN — no default DevAgent-supported advertised profile was found"
    else:
        certificate_text = "YES" if required else "NO"

    lines = [
        "",
        "CONNECTION GUIDANCE",
        f"Certificate required to connect: {certificate_text}",
        f"Certificate-free profile available: {'YES' if guidance.certificate_free_connection_available else 'NO'}",
        f"Secure supported profile available: {'YES' if guidance.secure_connection_available else 'NO'}",
        f"Anonymous authentication available: {'YES' if guidance.anonymous_available else 'NO'}",
        f"Username/password available by default policy: {'YES' if guidance.username_password_available else 'NO'}",
        (
            "Username/password NoSecurity profile: "
            + ("YES — explicit insecure opt-in required" if guidance.insecure_username_password_advertised else "NO")
        ),
        f"X.509 user-certificate authentication available: {'YES' if guidance.user_certificate_available else 'NO'}",
        f"IssuedToken/JWT advertised: {'YES (runtime unavailable)' if guidance.issued_token_advertised else 'NO'}",
    ]

    recommended = guidance.recommended
    if recommended is None:
        lines.append("Recommended profile: NONE")
        if guidance.insecure_username_password_advertised:
            lines.extend(
                [
                    "Action: this server advertises username/password only on a NoSecurity profile.",
                    "If that is the customer's intentional existing configuration, supply --username, --password-env, and --allow-insecure-username-password explicitly.",
                    "DevAgent will never enable or select the insecure username profile silently.",
                ]
            )
        else:
            lines.append(
                "Action: review the advertised security policy, security mode, and identity-token requirements."
            )
        return lines

    heading = (
        "Recommended production profile:"
        if recommended.secure_channel and not recommended.deprecated_policy
        else "Recommended available profile:"
    )
    lines.extend(
        [
            heading,
            f"  Endpoint: {recommended.endpoint.endpoint_url}",
            f"  Security: {recommended.policy_name} / {recommended.mode_name}",
            f"  Profile status: {recommended.support_status}",
            f"  Authentication: {recommended.authentication_summary}",
            (
                "  Client application certificate: "
                + ("REQUIRED" if recommended.application_certificate_required else "NOT REQUIRED")
            ),
        ]
    )
    if recommended.deprecated_policy:
        lines.append(
            "  Warning: this security policy is deprecated by OPC UA and is provided for older-server compatibility."
        )
    if recommended.application_certificate_required:
        lines.extend(
            [
                "  Required secure-channel files:",
                "    - client application certificate",
                "    - client application private key",
                "    - pinned server certificate",
            ]
        )
    if recommended.username_password_available and not recommended.username_password_requires_insecure_opt_in:
        lines.append("  Password: provide through --password-env; never place it directly on argv.")
    if recommended.user_certificate_available:
        prefix = "REQUIRED" if recommended.user_certificate_required else "AVAILABLE"
        lines.extend(
            [
                f"  X.509 user identity: {prefix}; provide --user-certificate and --user-private-key when selected.",
                "  User-key password, when needed: provide through --user-private-key-password-env.",
            ]
        )
    if not recommended.secure_channel:
        lines.extend(
            [
                "  Next step: browse/read can be attempted directly after supplying any required user-identity material.",
                "  Note: NoSecurity is appropriate for lab/legacy use; production should prefer a supported secure endpoint when available.",
            ]
        )
    return lines
