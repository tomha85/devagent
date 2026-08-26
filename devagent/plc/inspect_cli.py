from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import plc_jsonable
from devagent.plc.rockwell_l5x import L5XError
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent plc inspect",
        description=(
            "Read-only Rockwell Studio 5000 project discovery and semantic coverage inspection. "
            "No controller/project writes are performed."
        ),
    )
    parser.add_argument("project", type=Path, help="Rockwell Studio 5000 full-project .L5X export")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete machine-readable semantic coverage manifest",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the semantic coverage manifest to this new JSON file",
    )
    return parser


def _pct(value) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def _render(manifest: dict[str, object]) -> str:
    project = manifest["project"]
    inventory = manifest["inventory"]
    summary = manifest["instruction_summary"]
    languages = manifest["language_summary"]
    boundaries = manifest["project_boundaries"]
    rll = languages["rll"]
    st = languages["structured_text"]
    aoi = languages["aoi"]
    warnings = boundaries["warnings"]

    lines = [
        "DEVAGENT PLC PROJECT INSPECTION",
        "",
        f"Vendor: {project['vendor']}",
        f"Engineering tool: {project['engineering_tool']}",
        f"Controller: {project['controller']}",
        f"Processor: {project['processor_type'] or 'unknown'}",
        f"Source SHA-256: {project['source_sha256']}",
        "",
        "PROJECT INVENTORY",
        f"Tags: {inventory['tags']}",
        f"Data types: {inventory['data_types']}",
        f"Modules: {inventory['modules']}",
        f"Tasks: {inventory['tasks']}",
        f"Scheduled program entries: {inventory['scheduled_program_entries']}",
        f"Programs: {inventory['programs']}",
        f"Routines: {inventory['routines']}",
        f"Program RLL rungs: {inventory['program_rll_rungs']}",
        f"Structured Text statements: {inventory['structured_text_statements']}",
        f"AOIs: {inventory['aois']}",
        f"Output logic objects: {inventory['output_logic_objects']}",
        f"Analysis warnings: {len(warnings)}",
        "",
        "SEMANTIC COVERAGE",
        f"Program RLL instruction occurrences: {summary['total_occurrences']}",
        f"Program RLL deterministic instruction coverage: {_pct(summary['deterministic_pct'])}",
        f"Program RLL structural-or-better coverage: {_pct(summary['structural_or_better_pct'])}",
        f"Program RLL structural-only occurrences: {summary['structural_only_occurrences']}",
        f"Program RLL partial occurrences: {summary['partial_occurrences']}",
        f"Program RLL unmodeled occurrences: {summary['unmodeled_occurrences']} ({_pct(summary['unmodeled_pct'])})",
        "",
        "LANGUAGES / EXECUTION SURFACES",
        f"Program RLL rungs: {rll['program_rungs']}",
        f"Deterministic Boolean RLL rungs: {rll['deterministic_boolean_rungs']}",
        f"Bounded typed-compare rungs: {rll['bounded_compare_rungs']}",
        f"RLL branch coverage: {_pct(rll['branch_coverage_pct'])}",
        f"ST statements: {st['statements']}",
        f"ST reachable FULL dataflow: {st['reachable_full_dataflow_statements']} ({_pct(st['reachable_full_dataflow_pct'])})",
        f"ST partial/unreachable: {st['partial_or_unreachable_statements']}",
        f"ST opaque: {st['opaque_statements']}",
        f"ST parser-level semantic recognition: {st['parser_semantic_count']}/{st['statements']}",
        f"AOI definitions: {aoi['definitions']} (protected: {aoi['protected_definitions']})",
        f"AOI internal bodies modeled: {aoi['internal_bodies_modeled']}/{aoi['internal_bodies_total']}",
        f"AOI internal RLL statements: {aoi['internal_rll_full_statements']}/{aoi['internal_rll_statements']} FULL",
        f"AOI internal ST statements: {aoi['internal_st_full_statements']}/{aoi['internal_st_statements']} FULL",
        f"AOI calls bound: {aoi['calls_bound']}/{aoi['calls_total']}",
        "",
        "BOUNDARIES",
        "Partially modeled instructions: "
        + (", ".join(boundaries["partially_modeled_instruction_names"]) or "none"),
        "Unmodeled instructions: "
        + (", ".join(boundaries["unmodeled_instruction_names"]) or "none"),
        "Unsupported routine types: "
        + (", ".join(f"{name}={count}" for name, count in boundaries["unsupported_routine_types"].items()) or "none"),
        f"Protected routines: {boundaries['protected_routines']}",
        f"Warnings: {len(warnings)}",
    ]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings[:20])
        if len(warnings) > 20:
            lines.append(f"- ... {len(warnings) - 20} additional warning(s) available in --json/--output manifest")
    lines += [
        "",
        "PROGRAM RLL INSTRUCTION BREAKDOWN",
    ]
    for item in manifest["instructions"]:
        levels = ", ".join(f"{level}={count}" for level, count in item["levels"].items())
        lines.append(f"- {item['instruction']}: {item['occurrences']} ({levels})")
    lines += [
        "",
        "TRUST BOUNDARY",
        str(manifest["trust_note"]),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        engineering = analyze_rockwell_l5x(args.project)
        manifest = build_semantic_coverage_manifest(engineering.project)
        if args.output is not None:
            target = args.output.expanduser().resolve(strict=False)
            if target.exists():
                raise ValueError(f"Refusing to overwrite existing coverage artifact: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(plc_jsonable(manifest), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        if args.json:
            print(json.dumps(plc_jsonable(manifest), indent=2, ensure_ascii=False))
        else:
            print(_render(manifest))
        return 0
    except (L5XError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DevAgent PLC inspection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
