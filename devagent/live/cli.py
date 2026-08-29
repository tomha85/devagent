from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from .cli_security import add_security_args, security_from_args
from .connection_guidance import analyze_connection_guidance, format_connection_guidance
from .errors import LiveError
from .models import BrowseNode, RuntimeValue
from .opcua_client import ReadOnlyOpcUaClient
from .simulator import OpcUaSimulator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent live",
        description="Read-only OPC UA commissioning foundation for DevAgent.",
        allow_abbrev=False,
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

    vendor_qualify = subparsers.add_parser(
        "vendor-qualify",
        help=(
            "Qualify real Rockwell, Siemens, and Schneider engineering projects against their "
            "configured read-only OPC UA endpoints."
        ),
    )
    vendor_qualify.add_argument(
        "config",
        type=Path,
        help="devagent-live-commission-v1 config containing the real vendor project/endpoints.",
    )
    vendor_qualify.add_argument(
        "--output-dir",
        type=Path,
        help="Write vendor qualification evidence and manifest to a new directory.",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Check production Live install/runtime, project parsing, security, and a real OPC UA endpoint.",
        allow_abbrev=False,
    )
    doctor.add_argument("--project", type=Path, help="Optional onsite PLC engineering export to parse.")
    doctor.add_argument("--endpoint", help="Optional real read-only OPC UA endpoint to discover/connect.")
    doctor.add_argument(
        "--output-parent",
        type=Path,
        help="Directory to verify for evidence read/write access. Default: current directory.",
    )
    doctor.add_argument(
        "--output-dir",
        type=Path,
        help="Write doctor report and manifest to a new directory.",
    )
    add_security_args(doctor)

    soak = subparsers.add_parser(
        "soak",
        help="Run a long-lived read-only multi-PLC quality/recovery/resource soak.",
    )
    soak.add_argument("config", type=Path, help="Path to devagent-live-commission-v1 JSON config.")
    soak.add_argument(
        "--duration-hours",
        type=float,
        default=8.0,
        help="Requested soak duration in hours. Default: 8.",
    )
    soak.add_argument("--interval-seconds", type=float, default=1.0)
    soak.add_argument("--min-current-ratio", type=float, default=0.95)
    soak.add_argument("--max-consecutive-error-cycles", type=int, default=5)
    soak.add_argument("--max-memory-growth-mb", type=float, default=256.0)
    soak.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New directory for the soak evidence artifact and manifest.",
    )

    readiness = subparsers.add_parser(
        "readiness",
        help="Evaluate the 10-control Live production-readiness scorecard.",
    )
    readiness.add_argument(
        "--qualification-report",
        type=Path,
        help=(
            "Optional live_release_qualification.json artifact. Without it, the real-runtime "
            "control remains BLOCKED and the maximum valid rating is 9/10 PRODUCTION_CANDIDATE."
        ),
    )
    readiness.add_argument(
        "--output-dir",
        type=Path,
        help="Write the readiness report and SHA-256 manifest to a new directory.",
    )

    commercial = subparsers.add_parser(
        "commercial-readiness",
        help="Evaluate the strict five-gate DevAgent Live Commercial V1 readiness contract.",
    )
    commercial.add_argument("--runtime-qualification", type=Path)
    commercial.add_argument("--vendor-qualification", type=Path)
    commercial.add_argument("--doctor-report", type=Path)
    commercial.add_argument("--soak-report", type=Path)
    commercial.add_argument(
        "--min-soak-hours",
        type=float,
        default=8.0,
        help="Minimum accepted real soak duration. Default: 8 hours.",
    )
    commercial.add_argument(
        "--output-dir",
        type=Path,
        help="Write the commercial readiness report and manifest to a new directory.",
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
    guidance = analyze_connection_guidance(endpoints)
    print("DEVAGENT LIVE OPC UA PROBE")
    print(f"Endpoint: {args.endpoint}")
    print(f"Reachable: {'YES' if endpoints else 'NO'}")
    print(f"Endpoints discovered: {len(endpoints)}")
    for index, (endpoint, assessment) in enumerate(
        zip(endpoints, guidance.assessments, strict=True), start=1
    ):
        tokens = ", ".join(endpoint.user_token_types) or "-"
        print(f"\n[{index}] {endpoint.endpoint_url}")
        print(f"Server: {endpoint.server_application_name or '-'}")
        print(f"Security mode: {endpoint.security_mode or '-'}")
        print(f"Security policy: {endpoint.security_policy_uri or '-'}")
        print(f"User token types: {tokens}")
        print(f"DevAgent profile: {'SUPPORTED' if assessment.supported else 'UNSUPPORTED'}")
        print(f"Client certificate: {'REQUIRED' if assessment.certificate_required else 'NOT REQUIRED'}")
        print(f"Authentication: {assessment.authentication_summary}")
        print(f"Assessment: {assessment.reason}")
    for line in format_connection_guidance(guidance):
        print(line)
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


async def _run_vendor_qualify(args: argparse.Namespace) -> int:
    from .commission import load_commissioning_config
    from .vendor_qualification import (
        LiveVendorQualificationStatus,
        run_live_vendor_qualification,
        write_live_vendor_qualification_artifacts,
    )

    config = load_commissioning_config(args.config)
    report = await run_live_vendor_qualification(config)
    print("DEVAGENT LIVE REAL VENDOR QUALIFICATION")
    print("Mode: READ ONLY")
    print(f"Config SHA-256: {config.source_sha256}")
    print()
    for vendor in report.vendors:
        print(
            f"[{vendor.status.value}] {vendor.vendor}: PLCs={len(vendor.plc_ids)} "
            f"complete={vendor.complete_plcs} current={vendor.definitive_current_evidence} "
            f"mapped={vendor.accepted_mappings} unresolved={vendor.unresolved_mappings}"
        )
        print(f"  {vendor.detail}")
    print()
    print(f"Overall: {report.status.value}")
    if args.output_dir is not None:
        written = write_live_vendor_qualification_artifacts(args.output_dir, report)
        print(f"Artifacts: {written}")
    return 0 if report.status is LiveVendorQualificationStatus.PASS else 2


async def _run_doctor(args: argparse.Namespace) -> int:
    from .doctor import LiveDoctorStatus, run_live_doctor, write_live_doctor_artifacts

    report = await run_live_doctor(
        project_path=args.project,
        endpoint=args.endpoint,
        security=security_from_args(args),
        output_parent=args.output_parent,
    )
    print("DEVAGENT LIVE DOCTOR")
    print("Mode: READ ONLY")
    print()
    for check in report.checks:
        print(f"[{check.status.value}] {check.check_id} {check.title} - {check.detail}")
    counts = report.counts()
    print()
    print(
        f"Overall: {report.status.value} "
        f"(PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']})"
    )
    if args.output_dir is not None:
        written = write_live_doctor_artifacts(args.output_dir, report)
        print(f"Artifacts: {written}")
    return 0 if report.status is LiveDoctorStatus.PASS else 2


async def _run_soak(args: argparse.Namespace) -> int:
    from .commission import load_commissioning_config
    from .soak import LiveSoakStatus, run_live_soak, write_live_soak_artifacts

    if args.duration_hours <= 0:
        raise ValueError("--duration-hours must be > 0")
    config = load_commissioning_config(args.config)
    report = await run_live_soak(
        config,
        duration_seconds=args.duration_hours * 3600.0,
        interval_seconds=args.interval_seconds,
        min_current_ratio=args.min_current_ratio,
        max_consecutive_error_cycles=args.max_consecutive_error_cycles,
        max_memory_growth_mb=args.max_memory_growth_mb,
    )
    print("DEVAGENT LIVE PRODUCTION SOAK")
    print("Mode: READ ONLY")
    print(f"Requested duration: {args.duration_hours:.3f}h")
    print(f"Actual duration: {report.actual_duration_seconds / 3600.0:.3f}h")
    print(
        f"RSS start={report.memory_start_mb:.1f}MiB peak={report.memory_peak_mb:.1f}MiB "
        f"growth={report.memory_growth_mb:.1f}MiB"
    )
    print()
    for item in report.plcs:
        print(
            f"[{item.status.value}] {item.plc_id}: cycles={item.cycles} "
            f"current_ratio={item.current_ratio:.4f} errors={item.read_error_cycles} "
            f"max_consecutive_errors={item.max_consecutive_error_cycles} final={item.final_state}"
        )
        print(f"  {item.detail}")
    if report.setup_error:
        print(f"Setup/overall limitation: {report.setup_error}")
    print()
    print(f"Overall: {report.status.value}")
    written = write_live_soak_artifacts(args.output_dir, report)
    print(f"Artifacts: {written}")
    return 0 if report.status is LiveSoakStatus.PASS else 2


async def _run_readiness(args: argparse.Namespace) -> int:
    from .readiness import (
        evaluate_live_production_readiness,
        write_live_production_readiness_artifacts,
    )

    qualification = args.qualification_report if args.qualification_report is not None else None
    report = evaluate_live_production_readiness(qualification)
    print("DEVAGENT LIVE PRODUCTION READINESS")
    print("Mode: READ ONLY")
    print()
    for control in report.controls:
        print(f"[{control.status.value}] {control.control_id} {control.title} - {control.detail}")
    counts = report.counts()
    print()
    print(f"Score: {report.score}/{report.max_score}")
    print(f"Rating: {report.rating.value}")
    print(f"Production qualified: {'YES' if report.production_qualified else 'NO'}")
    print(
        f"Controls: PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']}"
    )
    if report.meets_nine_of_ten and not report.production_qualified:
        print("Runtime qualification point reserved: run `devagent live qualify` to earn 10/10.")
    if args.output_dir is not None:
        written = write_live_production_readiness_artifacts(args.output_dir, report)
        print(f"Artifacts: {written}")
    return 0 if report.meets_nine_of_ten else 2


async def _run_commercial_readiness(args: argparse.Namespace) -> int:
    from .commercial_readiness import (
        evaluate_live_commercial_readiness,
        write_live_commercial_readiness_artifacts,
    )

    report = evaluate_live_commercial_readiness(
        runtime_qualification_path=args.runtime_qualification,
        vendor_qualification_path=args.vendor_qualification,
        doctor_path=args.doctor_report,
        soak_path=args.soak_report,
        min_soak_hours=args.min_soak_hours,
    )
    print("DEVAGENT LIVE COMMERCIAL V1 READINESS")
    print("Mode: READ ONLY")
    print()
    for gate in report.gates:
        print(f"[{gate.status.value}] {gate.gate_id} {gate.title} - {gate.detail}")
    print()
    print(f"Overall: {report.status.value}")
    print(f"Commercial V1 ready: {'YES' if report.commercial_v1_ready else 'NO'}")
    if args.output_dir is not None:
        written = write_live_commercial_readiness_artifacts(args.output_dir, report)
        print(f"Artifacts: {written}")
    return 0 if report.commercial_v1_ready else 2


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
                print("For engineering-aware natural-language diagnosis use `devagent live assist <PLC_PROJECT> --endpoint ...`.")
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
                "This endpoint-only shell has no PLC engineering logic context. "
                "Use :read/:browse here, or `devagent live assist` for commissioning Q&A."
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
    if args.command == "vendor-qualify":
        return await _run_vendor_qualify(args)
    if args.command == "doctor":
        return await _run_doctor(args)
    if args.command == "soak":
        return await _run_soak(args)
    if args.command == "readiness":
        return await _run_readiness(args)
    if args.command == "commercial-readiness":
        return await _run_commercial_readiness(args)
    if args.command == "sim":
        return await _run_sim(args)
    if args.endpoint:
        return await _run_shell(args.endpoint, args)
    raise ValueError(
        "Use --endpoint for an endpoint-only interactive session, or choose "
        "probe/browse/read/snapshot/watch/plan/commission/qualify/vendor-qualify/doctor/soak/readiness/commercial-readiness/sim."
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