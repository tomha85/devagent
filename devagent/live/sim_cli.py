from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from .errors import LiveError
from .simulator import OpcUaSimulator
from .simulator_scenarios import SIMULATOR_SCENARIO_NAMES, SIMULATOR_SCENARIOS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent live sim",
        description=(
            "Run deterministic read-only OPC UA commissioning scenarios with known ground truth "
            "for DevAgent Live diagnosis qualification."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--endpoint",
        default="opc.tcp://127.0.0.1:4840/devagent/simulator/",
    )
    parser.add_argument(
        "--scenario",
        choices=SIMULATOR_SCENARIO_NAMES,
        default="normal",
        help="Known-ground-truth commissioning scenario. Default: normal.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List scenario names, expected health, and ground-truth intent without starting a server.",
    )
    parser.add_argument(
        "--update-interval-seconds",
        type=float,
        default=0.20,
        help="Simulator update interval. Default: 0.20 seconds.",
    )
    return parser


def _print_scenarios() -> None:
    print("DEVAGENT LIVE REALISTIC SIMULATOR MATRIX")
    print("Mode: READ ONLY")
    print()
    for name in SIMULATOR_SCENARIO_NAMES:
        spec = SIMULATOR_SCENARIOS[name]
        print(f"{name}")
        print(f"  Expected health: {spec.expected_system_health}")
        print(f"  Ground truth: {spec.expected_primary_reason}")
        print(f"  {spec.description}")


async def _run(args: argparse.Namespace) -> int:
    if args.list_scenarios:
        _print_scenarios()
        return 0

    simulator = OpcUaSimulator(
        args.endpoint,
        scenario=args.scenario,
        update_interval_seconds=args.update_interval_seconds,
    )
    await simulator.start()
    print("DEVAGENT LIVE OPC UA SIMULATOR")
    print(f"Endpoint: {simulator.endpoint}")
    print(f"Scenario: {simulator.scenario}")
    print(f"Description: {simulator.scenario_description}")
    print(f"Expected system health: {simulator.expected_system_health}")
    print(f"Expected primary reason: {simulator.expected_primary_reason}")
    print("Mode: READ ONLY")
    print("Variables are READ ONLY for OPC UA clients.")
    print()
    print("Known ground truth at scenario start:")
    for line in simulator.scenario_spec.ground_truth_lines():
        print(f"- {line}")
    if simulator.scenario_spec.dynamic:
        print("- Dynamic scenario: downstream/run values will transition after startup.")
    print()
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await simulator.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nDevAgent Live simulator interrupted; closing the OPC UA server.")
        return 130
    except (LiveError, OSError, ValueError) as exc:
        parser.exit(2, f"devagent live sim: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
