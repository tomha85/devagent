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
from .project_folder import LiveProjectFolderIntake, inspect_live_project_folder
from .realtime_manager import RealtimeMultiPlcConnectionManager
from .recursive_diagnosis import DEFAULT_TRACE_MAX_DEPTH, DEFAULT_TRACE_MAX_NODES
from .semantic_assistant import SemanticLiveCommissioningAssistant


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
        nargs="?",
        type=Path,
        help=(
            "Direct PLC engineering export: Rockwell .L5X, Siemens TIA exported source/XML "
            "bundle, or Schneider Control Expert .XEF/X* export. Omit this when using "
            "--project-folder."
        ),
    )
    parser.add_argument(
        "--project-folder",
        type=Path,
        help=(
            "Customer engineering workspace containing PLC logic/export plus optional I/O lists, "
            "tag descriptions, requirements, FAT files, drawings, and other supporting files. "
            "Live selects one authoritative supported PLC engineering source and inventories the rest as supplemental context."
        ),
    )
    parser.add_argument(
        "--primary-project",
        type=Path,
        help=(
            "Authoritative PLC engineering input inside --project-folder when auto-selection is ambiguous. "
            "Prefer a path relative to the project folder."
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
        help=(
            "Historical fallback/heartbeat interval in seconds. Realtime subscription events are "
            "captured between polls when available. Default: 1.0."
        ),
    )
    parser.add_argument(
        "--history-max-tags",
        type=int,
        default=64,
        help="Maximum safely reconciled tags retained in the rolling timeline. Default: 64.",
    )
    parser.add_argument(
        "--realtime-sampling-ms",
        type=float,
        default=100.0,
        help="OPC UA monitored-item sampling interval for Live realtime evidence. Default: 100 ms.",
    )
    parser.add_argument(
        "--realtime-publishing-ms",
        type=float,
        default=250.0,
        help="OPC UA subscription publishing interval for Live realtime evidence. Default: 250 ms.",
    )
    parser.add_argument(
        "--realtime-cache-ms",
        type=float,
        default=250.0,
        help=(
            "Maximum age of a trusted subscription snapshot that may answer a current-state "
            "question without an extra OPC UA Read. Default: 250 ms."
        ),
    )
    parser.add_argument(
        "--realtime-max-skew-ms",
        type=float,
        default=250.0,
        help=(
            "Maximum timestamp skew across cached dependency values before Live forces a coherent "
            "multi-node OPC UA Read. Default: 250 ms."
        ),
    )
    parser.add_argument(
        "--realtime-max-tags",
        type=int,
        default=256,
        help="Maximum safely reconciled OPC UA nodes continuously monitored. Default: 256.",
    )
    parser.add_argument(
        "--no-realtime-subscription",
        action="store_true",
        help=(
            "Disable continuous OPC UA subscriptions and use coherent on-demand reads plus the "
            "historical polling fallback only."
        ),
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help=(
            "Enable provider-neutral natural-language intent routing plus evidence-bounded AI explanations. "
            "PLC diagnosis remains deterministic and authoritative; Live still works without AI."
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


def _resolve_project_input(
    args: argparse.Namespace,
) -> tuple[Path, LiveProjectFolderIntake | None]:
    direct = args.project
    folder = args.project_folder
    primary = args.primary_project
    if direct is not None and folder is not None:
        raise ValueError("Use either a direct project path or --project-folder, not both")
    if direct is None and folder is None:
        raise ValueError("Provide a PLC engineering project path or --project-folder")
    if primary is not None and folder is None:
        raise ValueError("--primary-project is only valid with --project-folder")
    if folder is not None:
        intake = inspect_live_project_folder(folder, primary_project=primary)
        return intake.primary_project, intake
    assert direct is not None
    return direct.expanduser().resolve(strict=False), None


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
    print(":status       Show OPC UA connection, security, and realtime evidence state")
    print(":overview     Show engineering, mapping, stateful, and history overview")
    print(":workspace    Show project-folder intake and authority boundary")
    print(":mappings     Show accepted/unresolved engineering-to-OPC-UA mapping counts")
    print(":refresh      Re-browse and reconcile engineering tags to OPC UA")
    print(":help         Show this help")
    print(":disconnect   End the read-only commissioning session")
    print()
    print("Ask commissioning questions naturally. With --ai, the configured LLM interprets free-form wording into a bounded intent/target; deterministic PLC logic and trusted OPC UA evidence still decide the result.")
    print("Examples:")
    print("  Is the system good?")
    print("  Anything I should worry about?")
    print("  What is wrong with the system?")
    print("  Why won't the conveyor run?")
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
    realtime_status = getattr(assistant.manager, "realtime_status", None)
    if callable(realtime_status):
        realtime = realtime_status(assistant.connection.plc_id)
        print(f"Realtime evidence: {realtime.source}")
        print(f"Realtime connection epoch: {realtime.connection_epoch}")
        print(f"Realtime monitored nodes: {realtime.monitored_nodes}")
        print(f"Realtime cached nodes: {realtime.cached_nodes}")
        print(f"Realtime event backlog: {realtime.event_backlog}")
        if realtime.max_timestamp_skew_seconds is not None:
            print(
                "Last snapshot max timestamp skew: "
                f"{realtime.max_timestamp_skew_seconds * 1000.0:.1f} ms"
            )
        if realtime.last_subscription_error:
            print(f"Realtime limitation: {realtime.last_subscription_error}")
    print("Mode: READ ONLY")
    print("PLC write capability: NOT AVAILABLE")
    if status.last_error:
        print(f"Last error: {status.last_error}")


async def _run_session(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    project_path, project_intake = _resolve_project_input(args)
    loaded = load_live_engineering_context(project_path)
    connection = PlcConnectionSpec(
        plc_id=args.plc_id,
        plc_name=args.plc_name,
        endpoint=args.endpoint,
        security=security_from_args(args),
    )
    manager = RealtimeMultiPlcConnectionManager(
        [connection],
        subscription_enabled=not args.no_realtime_subscription,
        sampling_interval_ms=args.realtime_sampling_ms,
        publishing_interval_ms=args.realtime_publishing_ms,
        cache_fresh_seconds=args.realtime_cache_ms / 1000.0,
        max_snapshot_skew_seconds=args.realtime_max_skew_ms / 1000.0,
        max_monitored_nodes=args.realtime_max_tags,
    )
    assistant = SemanticLiveCommissioningAssistant(
        loaded,
        connection,
        manager=manager,
        browse_max_depth=args.max_depth,
        browse_max_nodes=args.max_nodes,
        provider=provider,
        trace_max_depth=args.trace_max_depth,
        trace_max_nodes=args.trace_max_nodes,
        history_seconds=args.history_seconds,
        history_poll_seconds=args.history_poll_seconds,
        history_max_tags=args.history_max_tags,
    )
    assistant.project_workspace = project_intake

    try:
        await assistant.start()
        if assistant.reconciliation is not None:
            await manager.monitor_node_ids(
                connection.plc_id,
                (
                    mapping.selected_node_id
                    for mapping in assistant.reconciliation.accepted_mappings()
                    if mapping.selected_node_id is not None
                ),
            )
        print("DEVAGENT LIVE ASSIST")
        if project_intake is not None:
            print(f"Project workspace: {project_intake.root}")
            print(f"Authoritative engineering input: {project_intake.primary_project}")
            print(f"Workspace files discovered: {len(project_intake.files)}")
            print(f"Supplemental context files: {len(project_intake.supplemental_files)}")
        print(f"Engineering project: {loaded.source_path}")
        print(f"Vendor: {loaded.context.vendor or 'UNKNOWN'}")
        print(f"Controller: {loaded.context.controller_name or connection.display_name}")
        print(f"Endpoint: {connection.endpoint}")
        print("Mode: READ ONLY")
        print("PLC write capability: NOT AVAILABLE")
        print(
            "AI language routing + explanations: ENABLED"
            if provider is not None
            else "AI language routing + explanations: OFF (deterministic question routing and diagnosis remain available)"
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
            f"Realtime OPC UA evidence: {'OFF' if args.no_realtime_subscription else 'ENABLED'} "
            f"sampling={args.realtime_sampling_ms:g}ms "
            f"publishing={args.realtime_publishing_ms:g}ms "
            f"cache_fresh={args.realtime_cache_ms:g}ms "
            f"max_skew={args.realtime_max_skew_ms:g}ms"
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
            if line == ":workspace":
                if project_intake is None:
                    print("Project workspace: DIRECT ENGINEERING INPUT")
                    print(f"Authoritative PLC engineering input: {loaded.source_path}")
                    print("No project-folder supplemental inventory is active for this session.")
                else:
                    print(project_intake.render_text())
                continue
            if line == ":mappings":
                _print_mapping_summary(assistant)
                continue
            if line == ":refresh":
                reconciliation = await assistant.refresh_mapping()
                await manager.monitor_node_ids(
                    connection.plc_id,
                    (
                        mapping.selected_node_id
                        for mapping in reconciliation.accepted_mappings()
                        if mapping.selected_node_id is not None
                    ),
                )
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
