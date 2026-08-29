from __future__ import annotations

import inspect

import devagent.live as live
from devagent.live import assist_cli
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant


def test_recursive_diagnosis_is_public_live_api():
    assert live.RecursiveLiveCommissioningAssistant is RecursiveLiveCommissioningAssistant
    assert callable(live.create_recursive_live_commissioning_assistant)
    assert callable(live.required_tag_ids_for_recursive_output)
    assert callable(live.trace_recursive_diagnosis)
    assert live.DEFAULT_TRACE_MAX_DEPTH >= 1
    assert live.DEFAULT_TRACE_MAX_NODES >= 1


def test_live_assist_parser_enables_bounded_recursive_trace_defaults():
    parser = assist_cli._build_parser()
    args = parser.parse_args(
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
        ]
    )

    assert args.trace_max_depth == live.DEFAULT_TRACE_MAX_DEPTH
    assert args.trace_max_nodes == live.DEFAULT_TRACE_MAX_NODES


def test_live_assist_parser_rejects_password_abbreviation_with_recursive_options_present():
    parser = assist_cli._build_parser()
    try:
        parser.parse_args(
            [
                "warehouse.L5X",
                "--endpoint",
                "opc.tcp://127.0.0.1:4840/",
                "--password",
                "secret",
            ]
        )
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("literal --password must remain rejected")


def test_recursive_live_modules_expose_no_plc_control_calls():
    source = "\n".join(
        [
            inspect.getsource(live.recursive_diagnosis),
            inspect.getsource(live.recursive_assistant),
        ]
    ).casefold()
    for forbidden in (
        "write_value(",
        "set_value(",
        "call_method(",
        ".force(",
        ".reset(",
        ".download(",
        ".change_mode(",
    ):
        assert forbidden not in source


def test_live_assist_constructs_recursive_assistant():
    source = inspect.getsource(assist_cli._run_session)
    assert "RecursiveLiveCommissioningAssistant(" in source
