from __future__ import annotations

import pytest

from devagent.live.errors import LiveConfigurationError
from devagent.live.simulator import OpcUaSimulator


def test_authenticated_simulator_requires_secure_channel() -> None:
    with pytest.raises(LiveConfigurationError, match="SignAndEncrypt"):
        OpcUaSimulator(
            username="operator",
            password="secret",
        )


def test_secure_simulator_requires_certificate_and_private_key() -> None:
    with pytest.raises(LiveConfigurationError, match="both server certificate and private key"):
        OpcUaSimulator(server_certificate="server.der")

    with pytest.raises(LiveConfigurationError, match="both server certificate and private key"):
        OpcUaSimulator(server_private_key="server.pem")


def test_authenticated_simulator_rejects_sign_only_mode() -> None:
    with pytest.raises(LiveConfigurationError, match="SignAndEncrypt"):
        OpcUaSimulator(
            username="operator",
            password="secret",
            server_certificate="server.der",
            server_private_key="server.pem",
            security_mode="Sign",
        )


def test_secure_anonymous_simulator_configuration_is_allowed() -> None:
    simulator = OpcUaSimulator(
        server_certificate="server.der",
        server_private_key="server.pem",
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
    )
    assert simulator.secure is True
    assert simulator.username is None
