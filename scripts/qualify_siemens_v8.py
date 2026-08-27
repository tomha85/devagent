from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import analyze_plc_project
from devagent.plc.models import PLCSemanticState
from devagent.plc.siemens_identity_types_v8 import _preflight, siemens_capability_profile_v8
from devagent.plc.siemens_tia_v1 import SiemensInputError, _MAX_TOTAL_BYTES


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _identity_bundle(root: Path) -> dict[str, object]:
    _write(
        root / "Types.udt",
        '''
TYPE "AxisData"
STRUCT
    Enabled : Bool;
    Speed : Real;
    Samples : ARRAY[0..7] OF DInt;
END_STRUCT;
END_TYPE

TYPE MachineState : (Idle, Running, Fault);
END_TYPE
''',
    )
    _write(
        root / "Machine.db",
        '''
DATA_BLOCK "Machine DB"
VAR
    Axis : "AxisData";
    State : MachineState;
END_VAR
BEGIN
END_DATA_BLOCK
''',
    )
    _write(
        root / "Worker.scl",
        '''
FUNCTION_BLOCK "Worker Block"
VAR_INPUT
    "Run Cmd" : Bool;
END_VAR
VAR
    Axis : "AxisData";
END_VAR
BEGIN
    "Machine DB".Axis.Enabled := "Run Cmd";
END_FUNCTION_BLOCK
''',
    )
    result = analyze_plc_project(root)
    profile = siemens_capability_profile_v8(result.project)
    facts = result.project._siemens_v8_identity_facts

    if profile["enum_types"] < 1 or profile["array_types"] < 1:
        raise RuntimeError(f"V8 type identity coverage incomplete: {profile}")
    if profile["udt_types"] + profile["struct_types"] < 1:
        raise RuntimeError(f"V8 structured type identity missing: {profile}")
    if not any(
        item.scope.casefold() == "controller"
        and item.display_path.casefold() == "machine db.axis.enabled"
        for item in facts.symbols
    ):
        raise RuntimeError("quoted DB/UDT member did not receive canonical controller identity")
    binding = next(
        (
            item
            for item in facts.bindings
            if item.access == "WRITE"
            and item.raw_ref.casefold() == "machine db.axis.enabled"
        ),
        None,
    )
    if binding is None or binding.semantic_state is not PLCSemanticState.FULL:
        raise RuntimeError(f"symbolic DB member write was not canonically resolved: {binding}")
    return {
        "canonical_symbols": profile["canonical_symbols"],
        "canonical_types": profile["canonical_types"],
        "udt_types": profile["udt_types"],
        "struct_types": profile["struct_types"],
        "array_types": profile["array_types"],
        "enum_types": profile["enum_types"],
        "identity_contract": profile["identity_contract"],
        "quoted_db_member": binding.canonical_display,
    }


def _input_bounds(root: Path) -> dict[str, object]:
    allowed = root / "allowed.scl"
    with allowed.open("wb") as handle:
        handle.truncate(_MAX_TOTAL_BYTES)
    _preflight(allowed)

    oversized = root / "oversized.scl"
    with oversized.open("wb") as handle:
        handle.truncate(_MAX_TOTAL_BYTES + 1)
    rejected = False
    try:
        _preflight(oversized)
    except SiemensInputError:
        rejected = True
    if not rejected:
        raise RuntimeError("single-file Siemens input above 100 MiB was not rejected before read")
    return {
        "limit_bytes": _MAX_TOTAL_BYTES,
        "exact_limit_preflight": "PASS",
        "over_limit_preflight": "REJECTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify Siemens V8 canonical data/type identity")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v8-") as directory:
        root = Path(directory)
        identity_root = root / "identity"
        identity_root.mkdir()
        bounds_root = root / "bounds"
        bounds_root.mkdir()
        report = {
            "schema": "devagent-siemens-production-qualification-v8",
            "contract": (
                "Project-wide Siemens canonical symbol/type identity with deterministic block-local shadowing, "
                "controller/DB/member scope, quoted identifiers, UDT/STRUCT/ARRAY/ENUM inventory, and "
                "fail-closed unresolved/ambiguous identities. Dynamic ARRAY index behavior is not promoted to runtime proof."
            ),
            "canonical_identity": _identity_bundle(identity_root),
            "input_bounds": _input_bounds(bounds_root),
            "external_execution": False,
            "result": "PASS",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
