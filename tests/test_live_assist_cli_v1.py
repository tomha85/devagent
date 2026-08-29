from __future__ import annotations

import sys

import pytest

from devagent import entrypoint
from devagent.live import assist_cli


def test_assist_parser_requires_project_and_endpoint_and_defaults_read_only_shape():
    parser = assist_cli._build_parser()
    args = parser.parse_args(
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
        ]
    )

    assert str(args.project) == "warehouse.L5X"
    assert args.endpoint == "opc.tcp://127.0.0.1:4840/"
    assert args.plc_id == "plc1"
    assert args.ai is False
    assert args.max_depth == 4
    assert args.max_nodes == 500


def test_assist_parser_rejects_literal_password_flag():
    parser = assist_cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "warehouse.L5X",
                "--endpoint",
                "opc.tcp://127.0.0.1:4840/",
                "--password",
                "secret",
            ]
        )


def test_assist_parser_accepts_password_environment_reference_only():
    parser = assist_cli._build_parser()
    args = parser.parse_args(
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--username",
            "engineer",
            "--password-env",
            "PLC_PASSWORD",
            "--security-policy",
            "Basic256Sha256",
            "--security-mode",
            "SignAndEncrypt",
        ]
    )

    assert args.username == "engineer"
    assert args.password_env == "PLC_PASSWORD"
    assert not hasattr(args, "password")


def test_entrypoint_routes_live_assist_to_dedicated_cli(monkeypatch):
    calls = []

    def fake_main(argv=None):
        calls.append(list(argv or []))
        return 17

    monkeypatch.setattr(assist_cli, "main", fake_main)

    result = entrypoint.main(
        [
            "live",
            "assist",
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
        ]
    )

    assert result == 17
    assert calls == [
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
        ]
    ]


def test_live_assist_cli_has_no_control_command_words():
    source = assist_cli.__file__
    assert source
    text = open(source, "r", encoding="utf-8").read().casefold()
    for forbidden in (
        "write_value(",
        "set_value(",
        "call_method(",
        "force(",
        "reset(",
        "download(",
        "change_mode(",
    ):
        assert forbidden not in text


def test_plc_dispatch_order_remains_after_live_routes():
    source = open(entrypoint.__file__, "r", encoding="utf-8").read()
    assert 'arguments[0] == "plc" and arguments[1] == "inspect"' in source
    assert 'if arguments and arguments[0] == "plc":' in source
    assert source.index('arguments[0] == "plc" and arguments[1] == "inspect"') < source.index(
        'if arguments and arguments[0] == "plc":'
    )
