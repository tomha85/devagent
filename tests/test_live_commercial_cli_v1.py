from __future__ import annotations

import inspect

import devagent.live as live
from devagent.live import assist_cli
from devagent.live import cli as live_cli
import devagent.live.doctor as doctor_module
import devagent.live.history as history_module
import devagent.live.soak as soak_module
import devagent.live.stateful_context as stateful_module
import devagent.live.vendor_qualification as vendor_module


def test_new_commercial_live_apis_are_public():
    assert callable(live.run_live_vendor_qualification)
    assert callable(live.build_live_stateful_coverage)
    assert callable(live.diagnose_live_stateful_model)
    assert callable(live.run_live_doctor)
    assert callable(live.run_live_soak)
    assert callable(live.evaluate_live_commercial_readiness)
    assert callable(live.requested_history_seconds)
    assert live.REQUIRED_VENDOR_FAMILIES == ("ROCKWELL", "SIEMENS", "SCHNEIDER")


def test_main_live_parser_keeps_existing_and_adds_commercial_commands(tmp_path):
    parser = live_cli._build_parser()

    assert parser.parse_args(["qualify", "--list"]).command == "qualify"
    assert parser.parse_args(["readiness"]).command == "readiness"
    assert parser.parse_args(["vendor-qualify", "commission.json"]).command == "vendor-qualify"
    assert parser.parse_args(["doctor"]).command == "doctor"
    soak = parser.parse_args(
        ["soak", "commission.json", "--output-dir", str(tmp_path / "soak")]
    )
    assert soak.command == "soak"
    assert soak.duration_hours == 8.0
    commercial = parser.parse_args(["commercial-readiness"])
    assert commercial.command == "commercial-readiness"
    assert commercial.min_soak_hours == 8.0


def test_doctor_parser_accepts_security_after_subcommand():
    parser = live_cli._build_parser()
    args = parser.parse_args(
        [
            "doctor",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--security-policy",
            "Basic256Sha256",
            "--security-mode",
            "SignAndEncrypt",
            "--server-certificate",
            "server.der",
        ]
    )

    assert args.command == "doctor"
    assert args.endpoint == "opc.tcp://127.0.0.1:4840/"
    assert args.security_policy == "Basic256Sha256"
    assert args.security_mode == "SignAndEncrypt"


def test_literal_password_remains_rejected_on_doctor():
    parser = live_cli._build_parser()
    try:
        parser.parse_args(
            [
                "doctor",
                "--endpoint",
                "opc.tcp://127.0.0.1:4840/",
                "--password",
                "secret",
            ]
        )
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("literal --password must never be accepted")


def test_assist_enables_bounded_history_by_default():
    parser = assist_cli._build_parser()
    args = parser.parse_args(
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
        ]
    )

    assert args.history_seconds == 300.0
    assert args.history_poll_seconds == 1.0
    assert args.history_max_tags == 64
    assert args.trace_max_depth == live.DEFAULT_TRACE_MAX_DEPTH
    assert args.trace_max_nodes == live.DEFAULT_TRACE_MAX_NODES


def test_assist_can_disable_history_without_disabling_diagnosis():
    parser = assist_cli._build_parser()
    args = parser.parse_args(
        [
            "warehouse.L5X",
            "--endpoint",
            "opc.tcp://127.0.0.1:4840/",
            "--history-seconds",
            "0",
        ]
    )

    assert args.history_seconds == 0.0
    assert args.trace_max_depth >= 1


def test_commercial_modules_expose_no_plc_control_calls():
    source = "\n".join(
        inspect.getsource(module).casefold()
        for module in (
            doctor_module,
            history_module,
            soak_module,
            stateful_module,
            vendor_module,
        )
    )
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


def test_old_production_readiness_remains_distinct_from_commercial_gate():
    assert live.LiveProductionReadinessReport is not live.LiveCommercialReadinessReport
    assert live.evaluate_live_production_readiness is not live.evaluate_live_commercial_readiness
