from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

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


async def _connected_client(endpoint: str, *, stale_after: float = 5.0) -> ReadOnlyOpcUaClient:
    client = ReadOnlyOpcUaClient(endpoint, stale_after_seconds=stale_after)
    await client.connect()
    return client


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
    client = await _connected_client(args.endpoint)
    try:
        nodes = await client.browse(max_depth=args.max_depth, max_nodes=args.max_nodes)
        print("DEVAGENT LIVE BROWSE")
        print(f"Endpoint: {args.endpoint}")
        print(f"Nodes returned: {len(nodes)}")
        print("Mode: READ ONLY\n")
        for node in nodes:
            print(_format_node(node))
        return 0
    finally:
        await client.disconnect()


async def _run_read(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint, stale_after=args.stale_after)
    try:
        value = await client.read(args.node_id)
        print("DEVAGENT LIVE READ")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY\n")
        print(_format_value(value))
        return 0 if value.loaded_successfully else 2
    finally:
        await client.disconnect()


async def _run_snapshot(args: argparse.Namespace) -> int:
    client = await _connected_client(args.endpoint)
    try:
        nodes = await client.browse(max_depth=args.max_depth, max_nodes=args.max_nodes)
        values = await client.load_values(nodes, max_values=args.max_values)
        successful = sum(1 for value in values if value.loaded_successfully)
        failed = len(values) - successful
        print("DEVAGENT LIVE SNAPSHOT")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY")
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
    client = await _connected_client(args.endpoint)
    try:
        values = await client.collect_changes(
            args.node_id,
            count=args.count,
            timeout_seconds=args.timeout,
        )
        print("DEVAGENT LIVE WATCH")
        print(f"Endpoint: {args.endpoint}")
        print("Mode: READ ONLY")
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


async def _run_shell(endpoint: str) -> int:
    client = await _connected_client(endpoint)
    try:
        print("DEVAGENT LIVE")
        print(f"Endpoint: {endpoint}")
        print("Connection: CONNECTED")
        print("Mode: READ ONLY")
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
                print("Connection: CONNECTED")
                print("Mode: READ ONLY")
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
    if args.command == "sim":
        return await _run_sim(args)
    if args.endpoint:
        return await _run_shell(args.endpoint)
    raise ValueError("Use --endpoint for an interactive session, or choose probe/browse/read/snapshot/watch/sim.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nDevAgent Live interrupted; closing the OPC UA session.")
        return 130
    except (LiveError, ValueError) as exc:
        parser.exit(2, f"devagent live: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
