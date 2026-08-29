from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from devagent.config import ProviderConfig, load_config, provider_defaults
from devagent.providers import ModelProvider, ProviderError, create_provider

from .assistant import LiveCommissioningAssistant as BaseLiveCommissioningAssistant
from .cli_security import add_security_args, security_from_args
from .engineering_context import load_live_engineering_context
from .errors import LiveError
from .manager import PlcConnectionSpec
from .recursive_assistant import RecursiveLiveCommissioningAssistant
from .recursive_diagnosis import DEFAULT_TRACE_MAX_DEPTH, DEFAULT_TRACE_MAX_NODES


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent live assist",
        description=(
            "Read-only onsite PLC commissioning assistant using the engineering project "
            "as logic context and OPC UA as trusted runtime evidence."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "project",
        type=Path,
        help=(
            "PLC engineering export: Rockwell .L5X, Siemens TIA exported source/XML bundle, "
            "or Schneider Control Expert .XEF/X* export."
        ),
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="Read-only OPC UA opc.tcp:// endpoint for the onsite PLC.",
    )
    parser.add_argument(
        "--plc-id",
        default="plc1",
        help="Stable PLC identifier used for Live evidence. Default: plc1.",
    )
    parser.add_argument(
        "--plc-name",
        help="Optional human-readable PLC name.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=4,
        help="Maximum OPC UA browse depth used for tag reconciliation. Default: 4.",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=500,
        help="Maximum OPC UA nodes inspected for tag reconciliation. Default: 500.",
    )
    parser.add_argument(
        "--trace-max-depth",
        type=int,
        default=DEFAULT_TRACE_MAX_DEPTH,
        help=(
            "Maximum deterministic upstream PLC logic depth for recursive root-cause tracing. "
            f"Default: {DEFAULT_TRACE_MAX_DEPTH}."
        ),
    )
    parser.add_argument(
        "--trace-max-nodes",
        type=int,
        default=DEFAULT_TRACE_MAX_NODES,
        help=(
            "Maximum engineering/live signals considered by one recursive diagnosis. "
            f"Default: {DEFAULT_TRACE_MAX_NODES}."
        ),
    )
    parser.add_argument(
        "--history-seconds",
        type=float,
        default=300.0,
        help="Trusted rolling timeline retention in seconds. Use 0 to disable. Default: 300.",
    )
    parser.add_argument(
        "--history-poll-seconds",
        type=float,
        default=1.0,
        help="Read-only historical polling interval in seconds. Default: 1.0.",
    )
    parser.add_argument(
        "--history-max-tags",
        type=int,
        default=64,
        help="Maximum safely reconciled tags retained in the rolling timeline. Default: 64.",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help=(
            "Enable optional evidence-bounded AI explanations. Deterministic commissioning "
            "diagnosis remains authoritative and works without AI."
        ),
    )
    parser.add_argument(
        "--provider",
        help="Override configured AI provider for this Live assistant session.",
    )
    parser.add_argument(
        "--model",
        help="Override configured AI model for this Live assistant session.",
    )
    parser.add_argument(
        "--base-url",
        help="Override OpenAI-compatible base URL for this Live assistant session.",
    )
    add_security_args(parser)
    return parser


def _provider_from_args(args: argparse.Namespace) -> ModelProvider | None:
    if not args.ai:
        return None
    base = load_config()
    provider_name = str(args.provider or base.provider).lower()
    default_model, default_env = provider_defaults(provider_name)
    same_provider = provider_name == base.provider.lower()
    config = ProviderConfig(
        provider=provider_name,
        model=str(args.model or (base.model if same_provider else default_model)),
        base_url=(
            args.base_url
            if args.base_url is not None
            else (base.base_url if same_provider else None)
        ),
        api_key_env=(base.api_key_env if same_provider else default_env),
        timeout_seconds=base.timeout_seconds,
    )
    return create_provider(config)


def _print_help() -> None:
    print(":status       Show OPC UA connection and security state")
    print(":overview     Show engineering, mapping, stateful, and history overview")
    print(":mappings     Show accepted/unresolved engineering-to-OPC-UA mapping counts")
    print(":refresh      Re-browse and reconcile engineering tags to OPC UA")
    print(":help         Show this help")
    print(":disconnect   End the read-only commissioning session")
    print()
    print("Or ask a commissioning question, for example:")
    print("  Why is Conveyor7_Run not active?")
    print("  Which permissive is blocking Conveyor7_Run?")
    print("  Why is that permissive false?")
    print("  Why did Conveyor7_Run stop 30 seconds ago?")
    print("  Why is SequenceState not advancing?")
    print("  Why is Timer1 not done?")
    print("  What should I check next?")


def _print_mapping_summary(assistant: BaseLiveCommissioningAssistant) -> None:
    reconciliation = assistant.reconciliation
    if reconciliation is None:
        print("Tag reconciliation: NOT RUN")
        return
    accepted = reconciliation.accepted_mappings()
    unresolved = reconciliation.unresolved_mappings()
    print(f"Mapped tags: {len(accepted)}")
    print(f"Unresolved tags: {len(unresolved)}")
    if unresolved:
        print("Unresolved examples:")
        for mapping in unresolved[:12]:
            print(
                f"- {mapping.tag_name} ({mapping.tag_id}): "
                f"{mapping.status.value} - {mapping.reason}"
            )
        if len(unresolved) > 12:
            print(f"- ... {len(unresolved) - 12} more")


def _print_status(assistant: BaseLiveCommissioningAssistant) -> None:
    status = assistant.manager.status(assistant.connection.plc_id)
    print(f"PLC: {status.plc_name} ({status.plc_id})")
    print(f"Endpoint: {status.endpoint}")
    print(f"Connection: {status.state.value}")
    print(f"Authentication: {status.authentication_mode}")
    print(f"Security: {status.security_summary}")
    print("Mode: READ ONLY")
    print("PLC write capability: NOT AVAILABLE")
    if status.last_error:
        print(f"Last error: {status.last_error}")


async def _run_session(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    loaded = load_live_engineering_context(args.project)
    connection = PlcConnectionSpec(
        plc_id=args.plc_id,
        plc_name=args.plc_name,
        endpoint=args.endpoint,
        security=security_from_args(args),
    )
    assistant = RecursiveLiveCommissioningAssistant(
        loaded,
        connection,
        browse_max_depth=args.max_depth,
        browse_max_nodes=args.max_nodes,
        provider=provider,
        trace_max_depth=args.trace_max_depth,
        trace_max_nodes=args.trace_max_nodes,
        history_seconds=args.history_seconds,
        history_poll_seconds=args.history_poll_seconds,
        history_max_tags=args.history_max_tags,
    )

    try:
        await assistant.start()
        print("DEVAGENT LIVE ASSIST")
        print(f"Engineering project: {loaded.source_path}")
        print(f"Vendor: {loaded.context.vendor or 'UNKNOWN'}")
        print(f"Controller: {loaded.context.controller_name or connection.display_name}")
        print(f"Endpoint: {connection.endpoint}")
        print("Mode: READ ONLY")
        print("PLC write capability: NOT AVAILABLE")
        print(
            "AI explanations: ENABLED"
            if provider is not None
            else "AI explanations: OFF (deterministic diagnosis remains available)"
        )
        print(
            f"Recursive root-cause trace: ENABLED "
            f"(depth={assistant.trace_max_depth}, nodes={assistant.trace_max_nodes})"
        )
        print(
            f"Stateful context: models={len(assistant.stateful_coverage.models)} "
            f"timers={assistant.stateful_coverage.timers} "
            f"counters={assistant.stateful_coverage.counters} "
            f"state_machines={assistant.stateful_coverage.state_machines}"
        )
        print(
            f"Historical timeline: {'ENABLED' if assistant.history_seconds > 0 else 'OFF'} "
            f"retention={assistant.history_seconds:g}s"
        )
        _print_mapping_summary(assistant)
        print("Type :help for commands. Ask commissioning questions directly.\n")

        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                line = ":disconnect"
            line = line.strip()
            if not line:
                continue
            if line in {":disconnect", ":exit", ":quit"}:
                print("Disconnecting...")
                return 0
            if line == ":help":
                _print_help()
                continue
            if line == ":status":
                _print_status(assistant)
                continue
            if line in {":overview", ":context"}:
                print(assistant._overview_text())
                continue
            if line == ":mappings":
                _print_mapping_summary(assistant)
                continue
            if line == ":refresh":
                reconciliation = await assistant.refresh_mapping()
                print(
                    "Tag reconciliation refreshed: "
                    f"mapped={len(reconciliation.accepted_mappings())} "
                    f"unresolved={len(reconciliation.unresolved_mappings())}"
                )
                continue

            reply = await assistant.answer(line)
            print(reply.render_text())
    finally:
        try:
            await assistant.close()
        except Exception:
            pass
        print("OPC UA commissioning session closed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(_run_session(args))
    except KeyboardInterrupt:
        print("\nDevAgent Live Assist interrupted; closing the OPC UA session.")
        return 130
    except (LiveError, ProviderError, OSError, ValueError) as exc:
        parser.exit(2, f"devagent live assist: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
