from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devagent.live import cli
from devagent.live.models import EndpointSummary


@pytest.mark.parametrize(
    ("mode", "policy", "tokens", "expected_status"),
    [
        ("None_", "None", ("UserName",), "BLOCKED_BY_POLICY"),
        (
            "SignAndEncrypt",
            "Basic128Rsa15",
            ("Anonymous",),
            "DEPRECATED_COMPATIBILITY",
        ),
        (
            "SignAndEncrypt",
            "ECC_nistP256",
            ("Anonymous",),
            "RUNTIME_UNAVAILABLE",
        ),
    ],
)
def test_probe_renders_detailed_support_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    policy: str,
    tokens: tuple[str, ...],
    expected_status: str,
) -> None:
    policy_uri = (
        "http://opcfoundation.org/UA/SecurityPolicy#None"
        if policy == "None"
        else f"http://opcfoundation.org/UA/SecurityPolicy#{policy}"
    )
    endpoint = EndpointSummary(
        endpoint_url="opc.tcp://192.168.10.20:4840/",
        security_mode=mode,
        security_policy_uri=policy_uri,
        user_token_types=tokens,
        server_application_name="Test PLC OPC UA Server",
    )

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def discover_endpoints(self) -> tuple[EndpointSummary, ...]:
            return (endpoint,)

    monkeypatch.setattr(cli, "ReadOnlyOpcUaClient", FakeClient)

    result = asyncio.run(
        cli._run_probe(
            SimpleNamespace(
                endpoint="opc.tcp://192.168.10.20:4840/",
                timeout=1.0,
            )
        )
    )

    assert result == 0
    output = capsys.readouterr().out
    assert f"DevAgent profile: {expected_status}" in output
