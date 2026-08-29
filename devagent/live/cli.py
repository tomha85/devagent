from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from .cli_security import add_security_args, security_from_args
from .errors import LiveError
from .models import BrowseNode, RuntimeValue
from .opcua_client import ReadOnlyOpcUaClient
from .simulator import OpcUaSimulator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent live",
        description="Read-only OPC UA commissioning foundation for DevAgent.",
    )
    parser.add_argument(
        "--endpoint",
        help="OPC UA endpoint used by the persistent interactive shell.",
    )
    add_security_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    probe = subparsers.add_parser("probe", help="Discover endpoint security and identity options.")
    probe.add_argument("endpoint")
    probe.add_argument("--timeout", type=float, default=4.0)

    browse = subparsers.add_parser("browse", help="Browse a bounded OPC UA address-space subtree.")
    browse.add_argument("endpoint")
    browse.add_argument("--max-depth", type=int, default=4)
    browse.add_argument("--max-nodes", type=int, default=200)

    read = subparsers.add_parser("read", help="Read one OPC UA Variable as a full DataValue.")
    read.add_argument("endpoint")
    read.add_argument("node_id")
    read.add_argument("--stale-after", type=float, default=5.0)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Browse variables and verify that live values can be loaded successfully.",
    )
    snapshot.add_argument("endpoint")
    snapshot.add_argument("--max-depth", type=int, default=4)
    snapshot.add_argument("--max-nodes", type=int, default=200)
    snapshot.add_argument("--max-values", type=int, default=100)

    watch = subparsers.add_parser("watch", help="Collect a bounded number of live data-change notifications.")
    watch.add_argument("endpoint")
    watch.add_argument("node_id", nargs="+")
    watch.add_argument("--count", type=int, default=5)
    watch.add_argument("--timeout", type=float, default=10.0)

    plan = subparsers.add_parser(
        "plan",
        help="Generate a read-only commissioning config from FAT-derived engineering tag references.",
    )
    plan.add_argument("project", type=Path, help="Rockwell/Siemens/Schneider engineering export.")
    plan.add_argument("--plc-id", required=True, help="Stable PLC identifier for the generated commissioning config.")
    plan.add_argument("--plc-name", help="Optional human-readable PLC name.")
    plan.add_argument("--endpoint", required=True, help="Read-only OPC UA opc.tcp:// endpoint.")
    plan.add_argument("--output", required=True, type=Path, help="New devagent-live-commission-v1 JSON file to create.")

    commission = subparsers.add_parser(
        "commission",
        help=(
            "Validate and run a bounded multi-PLC read-only commissioning workflow "
            "from a JSON configuration."
        ),
    )
    commission.add_argument("config", type=Path, help="Path to devagent-live-commission-v1 JSON config.")
    commission.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate config, security files, engineering exports, and required tag IDs without connecting to PLCs.",
    )
    commission.add_argument(
        "--output-dir",
        type=Path,
        help="Write sanitized commissioning summary, mapping, evidence, and manifest artifacts here. Directory must not already exist.",
    )

    qualify = subparsers.add_parser(
        "qualify",
        help="Run the read-only Live OPC UA release qualification matrix.",
    )
    qualify.add_argument(
        "--list",
        action="store_true",
        help="List stable qualification case IDs without executing the matrix.",
    )
    qualify.add_argument(
        "--output-dir",
        type=Path,
        help="Write the qualification report and SHA-256 manifest to a new directory.",
    )

    sim = subparsers.add_parser("sim", help="Run the local DevAgent OPC UA qualification simulator.")
    sim.add_argument(
        "--endpoint",
        default="opc.tcp://127.0.0.1:4840/devagent/simulator/",
    )
    sim.add_argument("--scenario", choices=["normal", "blocker"], default="normal")

    return parser


def _format_node(node: BrowseNode) -> str:
    data_type = f" {node.data_type}" if node.data_type else ""
    access = ",".join(node.user_access) if node.user_access else "-"
    return f"{node.path:<56} {node.node_class:<10}{data_type:<14} access={access} id={node.node_id}"


def _format_value(value: RuntimeValue) -> str:
    age = "unknown" if value.age_seconds is None else f"{value.age_seconds * 1000:.1f} ms"
    source = value.source_timestamp.isoformat() if value.source_timestamp else "-"
    server = value.server_timestamp.isoformat() if value.server_timestamp else "-"
    load = "SUCCESS" if value.loaded_successfully else "FAILED"
    return "\n".join(
        [
            f"NodeId: {value.node_id}",
            f"Load: {load}",
            f"Value: {value.value!r}",
            f"Type: {value.variant_type or '-'}",
            f"Status: {value.status_code}",
            f"Quality: {value.quality.value}",
            f"Trust: {value.trust.value}",
            f"Source timestamp: {source}",
            f"Server timestamp: {server}",
            f"Age: {age}",
            f"Replayed: {'YES' if value.replayed else 'NO'}",
        ]
    )


async def _connected_client(
    endpoint: str,
    args: argparse.Namespace,
    *,
    stale_after: float = 5.0,
) -> ReadOnlyOpcUaClient:
    client = ReadOnlyOpcUaClient(
        endpoint,
        stale_after_seconds=stale_after,
        security=security_from_args(args),
    )
    await client.connect()
    return client


def _print_connection_security(client: ReadOnlyOpcUaClient) -> None:
    print(f"Authentication: {client.authentication_mode}")
    print(f"Security: {client.security_summary}")


async def _run_probe(args: argparse.Namespace) -> int:
    client = ReadOnlyOpcUaClient(args.endpoint, timeout_seconds=args.timeout, auto_reconnect=False)
    endpoints = await client.discover_endpoints()
    print("DEVAGENT LIVE OPC UA PROBE")
    print(f"Endpoint: {args.endpoint}")
    print(f"Reachable: {'YES' if endpoints else 'NO'}")
    print(f"Endpoints discovered: {len(endpoints)}")
    for index, endpoint in enumerate(endpoints, start=1):
        tokens = ", ".join(endpoint.user_token_types) or "-"
        print(f"\n[{index}] {endpoint.endpoint_url}")
        print(f"Server: {endpoint.server_application_name or '-'}")
        print(f"Security mode: {endpoint.security_mode or '-'}")
        print(f"Security policy: {endpoint.security_policy_uri or '-'}")
        print(f"User token types: {tokens}")
    return 0


async def _run_browse(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint, args)
    try:
        nodes = await client.browse(max_depth=args.max_depth, max_nodes=args.max_nodes)
        print("DEVAGENT LIVE BROWSE")
        print(f"Endpoint: {args.endpoint}")
        print(f"Nodes returned: {len(nodes)}")
        print("Mode: READ ONLY")
        _print_connection_security(client)
        print()
        for node in nodes:
            print(_format_node(node))
        return 0
    finally:
        await client.disconnect()


async def _run_read(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint, args, stale_after=args.stale_after)
    try:
        value = await client.read(args.node_id)
        print("DEVAGENT LIVE READ")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY")
        _print_connection_security(client)
        print()
        print(_format_value(value))
        return 0 if value.loaded_successfully else 2
    finally:
        await client.disconnect()


async def _run_snapshot(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint, args)
    try:
        nodes = await client.browse(max_depth=args.max_depth, max_nodes=args.max_nodes)
        values = await client.load_values(nodes, max_values=args.max_values)
        successful = sum(1 for value in values if value.loaded_successfully)
        failed = len(values) - successful
        print("DEVAGENT LIVE SNAPSHOT")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY")
        _print_connection_security(client)
        print(f"Variables attempted: {len(values)}")
        print(f"Loaded successfully: {successful}")
        print(f"Failed/untrusted: {failed}")
        for value in values:
            print(
                f"{value.node_id} value={value.value!r} type={value.variant_type} "
                f"quality={value.quality.value} trust={value.trust.value}"
            )
        return 0 if values and failed == 0 else 2
    finally:
        await client.disconnect()


async def _run_watch(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint, args)
    try:
        values = await client.collect_changes(
            args.node_id,
            count=args.count,
            timeout_seconds=args.timeout,
        )
        print("DEVAGENT LIVE WATCH")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY")
        _print_connection_security(client)
        print(f"Notifications: {len(values)}")
        for value in values:
            stamp = value.source_timestamp or value.server_timestamp or value.received_at
            print(
                f"{stamp.isoformat()} {value.node_id} value={value.value!r} "
                f"quality={value.quality.value} trust={value.trust.value}"
            )
        return 0 if values else 2
    finally:
        await client.disconnect()


async def _run_plan(args: argparse.Namespace) -> int:
    from .plan import analyze_and_build_live_commission_plan, write_live_commission_plan

    plan = analyze_and_build_live_commission_plan(
        args.project,
        plc_id=args.plc_id,
        plc_name=args.plc_name,
        endpoint=args.endpoint,
    )
    print("DEVAGENT LIVE PLAN")
    print(f"Project: {plan.engineering_project_path}")
    print(f"Vendor: {plan.vendor}")
    print(f"PLC: {plan.plc_id} ({plan.plc_name})")
    print(f"Endpoint: {plan.endpoint}")
    print("Mode: READ ONLY")
    print(f"FAT runtime references: {len(plan.references)}")
    print(f"Resolved required tags: {len(plan.required_tag_ids)}")
    print(f"Unresolved references: {len(plan.unresolved)}")
    if not plan.complete:
        for item in plan.unresolved:
            print(f"- {item.reference}: {item.status.value}: {item.reason}")
        print("Plan: INCOMPLETE — commissioning config was not written.")
        return 2

    config_path, report_path = write_live_commission_plan(args.output, plan)
    print("Plan: COMPLETE")
    print(f"Commission config: {config_path}")
    print(f"Plan provenance: {report_path}")
    print(f"Next: devagent live commission {config_path} --validate-only")
    return 0


async def _run_commission(args: argparse.Namespace) -> int:
    # Lazy import keeps ordinary probe/browse/read/watch/sim startup independent
    # from the vendor PLC engineering stack used to analyze commission exports.
    from .commission import (
        commissioning_summary,
        load_commissioning_config,
        run_loaded_commissioning_config,
        write_commissioning_artifacts,
    )

    config = load_commissioning_config(args.config)
    print("DEVAGENT LIVE COMMISSION")
    print(f"Config: {config.source_path}")
    print(f"Config SHA-256: {config.source_sha256}")
    print("Mode: READ ONLY")
    print(f"PLCs: {len(config.specs)}")
    for spec in config.specs:
        metadata = getattr(spec.engineering_project, "metadata", None)
        vendor = getattr(metadata, "vendor", "UNKNOWN")
        print(
            f"[{spec.connection.plc_id}] {spec.connection.display_name} "
            f"vendor={vendor} endpoint={spec.connection.endpoint} "
            f"required_tags={len(spec.required_tag_ids)} "
            f"auth={spec.connection.security.authentication_mode} "
            f"security={spec.connection.security.channel_summary}"
        )

    if args.validate_only:
        print("Validation: PASS")
        print("Network connection: NOT ATTEMPTED")
        return 0

    result = await run_loaded_commissioning_config(config)
    print()
    print("Commissioning results:")
    summary = commissioning_summary(config, result)
    for item in summary["plcs"]:
        error = f" error={item['error']}" if item["error"] else ""
        print(
            f"[{item['plc_id']}] state={item['state']} "
            f"connection={item['connection_state']} "
            f"current={item['definitive_current_evidence']} "
            f"excluded={item['excluded_raw_evidence']} "
            f"limitations={len(item['limitations'])}{error}"
        )

    if args.output_dir is not None:
        written = write_commissioning_artifacts(args.output_dir, config, result)
        print(f"Artifacts: {written}")
    return 0 if result.all_complete else 2


async def _run_qualify(args: argparse.Namespace) -> int:
    from .qualification import (
        LIVE_RELEASE_QUALIFICATION_CASES,
        LiveQualificationStatus,
        run_live_release_qualification,
        write_live_release_qualification_artifacts,
    )

    print("DEVAGENT LIVE RELEASE QUALIFICATION")
    print("Mode: READ ONLY")
    if args.list:
        for case in LIVE_RELEASE_QUALIFICATION_CASES:
            kind = "RUNTIME" if case.runtime_required else "DETERMINISTIC"
            print(f"{case.case_id} [{kind}] {case.title}")
        return 0

    report = await run_live_release_qualification()
    runtime = report.runtime_version or ("AVAILABLE" if report.runtime_available else "UNAVAILABLE")
    print(f"asyncua: {runtime}")
    print()
    for case in report.cases:
        print(f"[{case.status.value}] {case.case_id} {case.title} - {case.detail}")
    counts = report.counts()
    print()
    print(
        f"Overall: {report.status.value} "
        f"(PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']})"
    )
    if args.output_dir is not None:
        written = write_live_release_qualification_artifacts(args.output_dir, report)
        print(f"Artifacts: {written}")
    return 0 if report.status is LiveQualificationStatus.PASS else 2


async def _run_sim(args: argparse.Namespace) -> int:
    simulator = OpcUaSimulator(args.endpoint, scenario=args.scenario)
    await simulator.start()
    print("DEVAGENT LIVE OPC UA SIMULATOR")
    print(f"Endpoint: {simulator.endpoint}")
    print(f"Scenario: {simulator.scenario}")
    print("Variables are READ ONLY for OPC UA clients.")
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await simulator.stop()
    return 0


async def _run_shell(endpoint: str, args: argparse.Namespace) -> int:
    client = await _connected_client(endpoint, args)
    try:
        print("DEVAGENT LIVE")
        print(f"Endpoint: {endpoint}")
        print("Connection: CONNECTED")
        print("Mode: READ ONLY")
        _print_connection_security(client)
        print("PLC write capability: NOT AVAILABLE")
        print("Type :help for commands. :disconnect ends the session.\n")
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
                print(":status")
                print(":browse")
                print(":read <NodeId>")
                print(":disconnect")
                print(
                    "Natural-language diagnosis is intentionally deferred until the "
                    "OPC UA evidence foundation is qualified."
                )
                continue
            if line == ":status":
                print(f"Endpoint: {endpoint}")
                print(f"Connection: {client.connection_state}")
                print("Mode: READ ONLY")
                _print_connection_security(client)
                continue
            if line == ":browse":
                nodes = await client.browse(max_depth=3, max_nodes=100)
                for node in nodes:
                    print(_format_node(node))
                continue
            if line.startswith(":read "):
                node_id = line[len(":read ") :].strip()
                if not node_id:
                    print("Usage: :read <NodeId>")
                    continue
                print(_format_value(await client.read(node_id)))
                continue
            print(
                "Natural-language commissioning diagnosis is not enabled in this foundation milestone yet. "
                "The OPC UA session remains connected; use :read, :browse, or :status."
            )
    finally:
        try:
            await client.disconnect()
        finally:
            print("OPC UA session closed.")


async def _run(args: argparse.Namespace) -> int:
    if args.command == "probe":
        return await _run_probe(args)
    if args.command == "browse":
        return await _run_browse(args)
    if args.command == "read":
        return await _run_read(args)
    if args.command == "snapshot":
        return await _run_snapshot(args)
    if args.command == "watch":
        return await _run_watch(args)
    if args.command == "plan":
        return await _run_plan(args)
    if args.command == "commission":
        return await _run_commission(args)
    if args.command == "qualify":
        return await _run_qualify(args)
    if args.command == "sim":
        return await _run_sim(args)
    if args.endpoint:
        return await _run_shell(args.endpoint, args)
    raise ValueError(
        "Use --endpoint for an interactive session, or choose "
        "probe/browse/read/snapshot/watch/plan/commission/qualify/sim."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nDevAgent Live interrupted; closing the OPC UA session.")
        return 130
    except (LiveError, OSError, ValueError) as exc:
        parser.exit(2, f"devagent live: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
