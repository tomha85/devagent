from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import EndpointSummary
from .security import SUPPORTED_SECURITY_MODES, SUPPORTED_SECURITY_POLICIES


def _normalized(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def security_policy_name(uri: str) -> str:
    text = (uri or "").strip()
    if not text:
        return "None"
    if "#" in text:
        return text.rsplit("#", 1)[-1] or "None"
    return text.rsplit("/", 1)[-1] or text


def _token_set(endpoint: EndpointSummary) -> set[str]:
    return {_normalized(token) for token in endpoint.user_token_types}


@dataclass(frozen=True)
class EndpointConnectionAssessment:
    endpoint: EndpointSummary
    supported: bool
    secure_channel: bool
    certificate_required: bool
    anonymous_available: bool
    username_password_available: bool
    policy_name: str
    mode_name: str
    reason: str

    @property
    def authentication_summary(self) -> str:
        options: list[str] = []
        if self.username_password_available:
            options.append("USERNAME_PASSWORD")
        if self.anonymous_available:
            options.append("ANONYMOUS")
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
        return any(item.supported and not item.certificate_required for item in self.assessments)

    @property
    def secure_connection_available(self) -> bool:
        return any(item.supported and item.secure_channel for item in self.assessments)

    @property
    def username_password_available(self) -> bool:
        return any(item.supported and item.username_password_available for item in self.assessments)

    @property
    def anonymous_available(self) -> bool:
        return any(item.supported and item.anonymous_available for item in self.assessments)

    @property
    def certificate_required_to_connect(self) -> bool | None:
        supported = self.supported_assessments
        if not supported:
            return None
        return all(item.certificate_required for item in supported)


def assess_endpoint(endpoint: EndpointSummary) -> EndpointConnectionAssessment:
    mode_raw = (endpoint.security_mode or "").strip()
    mode_normalized = _normalized(mode_raw)
    mode_is_none = mode_normalized in {"", "none"}
    mode_name = "None" if mode_is_none else mode_raw
    policy_name = security_policy_name(endpoint.security_policy_uri)
    policy_normalized = _normalized(policy_name)
    policy_is_none = policy_normalized in {"", "none"}
    tokens = _token_set(endpoint)

    anonymous = "anonymous" in tokens
    username = "username" in tokens or "usernametoken" in tokens

    if mode_is_none and policy_is_none:
        supported = anonymous
        reason = (
            "Anonymous / NoSecurity is directly usable without certificates."
            if supported
            else "NoSecurity endpoint does not advertise Anonymous authentication."
        )
        return EndpointConnectionAssessment(
            endpoint=endpoint,
            supported=supported,
            secure_channel=False,
            certificate_required=False,
            anonymous_available=anonymous,
            username_password_available=False,
            policy_name="None",
            mode_name="None",
            reason=reason,
        )

    if mode_is_none != policy_is_none:
        return EndpointConnectionAssessment(
            endpoint=endpoint,
            supported=False,
            secure_channel=not mode_is_none,
            certificate_required=not mode_is_none,
            anonymous_available=anonymous,
            username_password_available=False,
            policy_name=policy_name,
            mode_name=mode_name or "-",
            reason="Endpoint advertises inconsistent security mode/policy metadata; refusing to infer a connection profile.",
        )

    policy_supported = policy_name in SUPPORTED_SECURITY_POLICIES
    mode_supported = mode_raw in SUPPORTED_SECURITY_MODES
    username_supported = username and mode_raw == "SignAndEncrypt"
    authentication_supported = anonymous or username_supported
    supported = policy_supported and mode_supported and authentication_supported

    if not policy_supported:
        reason = f"Security policy {policy_name!r} is not in DevAgent Live's supported policy set."
    elif not mode_supported:
        reason = f"Security mode {mode_raw or '-'} is not supported by DevAgent Live."
    elif username and not anonymous and mode_raw != "SignAndEncrypt":
        reason = "Username/password authentication requires SignAndEncrypt in DevAgent Live."
    elif not authentication_supported:
        reason = "Endpoint does not advertise a currently supported Anonymous or UserName identity token."
    else:
        reason = "Supported secure OPC UA profile; client certificate material is required."

    return EndpointConnectionAssessment(
        endpoint=endpoint,
        supported=supported,
        secure_channel=True,
        certificate_required=True,
        anonymous_available=anonymous,
        username_password_available=username_supported,
        policy_name=policy_name,
        mode_name=mode_raw or "-",
        reason=reason,
    )


def _recommendation_rank(item: EndpointConnectionAssessment) -> tuple[int, int, int, int]:
    policy_rank = {
        "Basic256Sha256": 1,
        "Aes128Sha256RsaOaep": 2,
        "Aes256Sha256RsaPss": 3,
    }.get(item.policy_name, 0)
    mode_rank = {"Sign": 1, "SignAndEncrypt": 2}.get(item.mode_name, 0)
    return (
        1 if item.secure_channel else 0,
        mode_rank,
        policy_rank,
        1 if item.username_password_available else 0,
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
        certificate_text = "UNKNOWN — no DevAgent-supported advertised profile was found"
    else:
        certificate_text = "YES" if required else "NO"

    lines = [
        "",
        "CONNECTION GUIDANCE",
        f"Certificate required to connect: {certificate_text}",
        f"Certificate-free profile available: {'YES' if guidance.certificate_free_connection_available else 'NO'}",
        f"Secure supported profile available: {'YES' if guidance.secure_connection_available else 'NO'}",
        f"Anonymous authentication available: {'YES' if guidance.anonymous_available else 'NO'}",
        f"Username/password available: {'YES' if guidance.username_password_available else 'NO'}",
    ]

    recommended = guidance.recommended
    if recommended is None:
        lines.extend(
            [
                "Recommended profile: NONE",
                "Action: review the advertised security policy, security mode, and identity-token requirements.",
            ]
        )
        return lines

    heading = (
        "Recommended production profile:"
        if recommended.secure_channel
        else "Recommended available profile:"
    )
    lines.extend(
        [
            heading,
            f"  Endpoint: {recommended.endpoint.endpoint_url}",
            f"  Security: {recommended.policy_name} / {recommended.mode_name}",
            f"  Authentication: {recommended.authentication_summary}",
            f"  Client certificate: {'REQUIRED' if recommended.certificate_required else 'NOT REQUIRED'}",
        ]
    )
    if recommended.certificate_required:
        lines.extend(
            [
                "  Required secure-channel files:",
                "    - client certificate",
                "    - client private key",
                "    - pinned server certificate",
            ]
        )
        if recommended.username_password_available:
            lines.append("  Password: provide through --password-env; never place it directly on argv.")
    else:
        lines.extend(
            [
                "  Next step: browse/read can be attempted directly without certificate flags.",
                "  Note: NoSecurity is appropriate for lab/simulator use; production should prefer a supported secure endpoint when available.",
            ]
        )
    return lines
