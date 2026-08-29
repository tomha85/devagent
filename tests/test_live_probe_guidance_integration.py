from __future__ import annotations

import argparse
import asyncio
import socket

import pytest

pytest.importorskip("asyncua")

from devagent.live.cli import _run_probe
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/simulator/"


def test_probe_explains_that_simulator_does_not_require_client_certificate(capsys) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        async with OpcUaSimulator(endpoint, scenario="normal"):
            rc = await _run_probe(argparse.Namespace(endpoint=endpoint, timeout=2.0))
            assert rc == 0

    asyncio.run(scenario())
    output = capsys.readouterr().out

    assert "DEVAGENT LIVE OPC UA PROBE" in output
    assert "DevAgent profile: SUPPORTED" in output
    assert "Client certificate: NOT REQUIRED" in output
    assert "CONNECTION GUIDANCE" in output
    assert "Certificate required to connect: NO" in output
    assert "Certificate-free profile available: YES" in output
    assert "Recommended available profile:" in output
    assert "NoSecurity is appropriate for lab/simulator use" in output
