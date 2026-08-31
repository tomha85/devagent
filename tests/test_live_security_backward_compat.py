from __future__ import annotations

from devagent.live.security import LiveSecurityConfig


def test_live_security_config_preserves_original_positional_field_order() -> None:
    config = LiveSecurityConfig(
        "operator",
        "secret",
        "Basic256Sha256",
        "SignAndEncrypt",
        "client.der",
        "client-key.pem",
        "key-secret",
        "server.der",
        "urn:legacy:caller",
    )

    assert config.username == "operator"
    assert config.security_policy == "Basic256Sha256"
    assert config.security_mode == "SignAndEncrypt"
    assert config.client_certificate == "client.der"
    assert config.client_private_key == "client-key.pem"
    assert config.server_certificate == "server.der"
    assert config.application_uri == "urn:legacy:caller"
    assert config.user_certificate is None
    assert config.allow_insecure_username_password is False
    assert "secret" not in repr(config)
    assert "key-secret" not in repr(config)
