from __future__ import annotations

from pathlib import Path

import pytest

from devagent.live.commission import _security_from_json
from devagent.live.errors import LiveConfigurationError


def _touch(path: Path) -> str:
    path.write_text("test", encoding="utf-8")
    return path.name


def test_commission_json_loads_x509_user_identity(tmp_path) -> None:
    client_cert = _touch(tmp_path / "client.der")
    client_key = _touch(tmp_path / "client.pem")
    server_cert = _touch(tmp_path / "server.der")
    user_cert = _touch(tmp_path / "user.der")
    user_key = _touch(tmp_path / "user.pem")

    security = _security_from_json(
        {
            "security_policy": "Basic256Sha256",
            "security_mode": "SignAndEncrypt",
            "client_certificate": client_cert,
            "client_private_key": client_key,
            "server_certificate": server_cert,
            "user_certificate": user_cert,
            "user_private_key": user_key,
            "user_private_key_password_env": "USER_KEY_PASSWORD",
        },
        base_dir=tmp_path,
        env={"USER_KEY_PASSWORD": "user-secret"},
        validate_files=True,
    )

    assert security.authentication_mode == "X509_USER_CERTIFICATE"
    assert security.user_private_key_password == "user-secret"
    assert security.client_certificate == str(tmp_path / "client.der")
    assert security.user_certificate == str(tmp_path / "user.der")
    assert "user-secret" not in repr(security)


def test_commission_json_loads_explicit_no_security_username_profile(tmp_path) -> None:
    security = _security_from_json(
        {
            "username": "operator",
            "password_env": "PLC_PASSWORD",
            "allow_insecure_username_password": True,
        },
        base_dir=tmp_path,
        env={"PLC_PASSWORD": "secret"},
        validate_files=True,
    )

    assert security.authentication_mode == "USERNAME_PASSWORD"
    assert security.allow_insecure_username_password is True
    assert security.insecure_username_password is True
    assert security.channel_summary == "NONE"


def test_commission_json_blocks_no_security_username_by_default(tmp_path) -> None:
    with pytest.raises(LiveConfigurationError, match="blocked by default"):
        _security_from_json(
            {"username": "operator", "password_env": "PLC_PASSWORD"},
            base_dir=tmp_path,
            env={"PLC_PASSWORD": "secret"},
            validate_files=True,
        )


def test_commission_json_rejects_literal_user_private_key_password(tmp_path) -> None:
    with pytest.raises(LiveConfigurationError, match="secret value field"):
        _security_from_json(
            {"user_private_key_password": "do-not-store"},
            base_dir=tmp_path,
            env={},
            validate_files=False,
        )
