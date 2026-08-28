from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCSemanticState
from devagent.plc.schneider_identity_types_v8 import schneider_capability_profile_v8


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _xdd() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<DDTExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8Qualification" version="1.0" />
  <DDT name="AxisData">
    <variables name="Enabled" typeName="BOOL" />
    <variables name="Samples" typeName="DINT" dimension="0..7" />
  </DDT>
</DDTExchangeFile>
'''


def _main() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8Qualification" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Run := Start;
Axis.Enabled := Start;
DirectRun := %I0.1;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" topologicalAddress="%I0.0" />
    <variables name="Run" typeName="BOOL" topologicalAddress="%Q0.0" />
    <variables name="DirectRun" typeName="BOOL" topologicalAddress="%Q0.1" />
    <variables name="Axis" typeName="AxisData" />
  </dataBlock>
</STExchangeFile>
'''


def _alias() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV8Alias" version="1.0" />
  <program>
    <identProgram name="Alias" type="section" task="MAST" />
    <STSource>
Y1 := A;
Y2 := B;
    </STSource>
  </program>
  <dataBlock>
    <variables name="A" typeName="BOOL" topologicalAddress="%M10" />
    <variables name="B" typeName="BOOL" topologicalAddress="%M10" />
    <variables name="Y1" typeName="BOOL" />
    <variables name="Y2" typeName="BOOL" />
  </dataBlock>
</STExchangeFile>
'''


def _identity_bundle(root: Path) -> dict[str, object]:
    _write(root / "Types.xdd", _xdd())
    _write(root / "Main.xst", _main())
    result = run_production_verification_v5(root)
    project = result.engineering.project
    profile = schneider_capability_profile_v8(project)
    facts = project._schneider_v8_identity_facts

    required = {
        ("controller", "axis.enabled"),
        ("controller", "axis.samples[*]"),
    }
    displays = {(item.scope.casefold(), item.display_path.casefold()) for item in facts.symbols}
    missing = required - displays
    if missing:
        raise RuntimeError(f"V8 canonical DDT/ARRAY identity missing: {sorted(missing)}")
    if ("controller", "enabled") in displays:
        raise RuntimeError("DDT member leaked into controller-root canonical identity")
    if profile["ddt_types"] < 1 or profile["array_types"] < 1:
        raise RuntimeError(f"V8 structured type inventory incomplete: {profile}")
    if profile["input_identities"] < 1 or profile["output_identities"] < 2:
        raise RuntimeError(f"V8 located I/O inventory incomplete: {profile}")
    direct = next((item for item in facts.bindings if item.raw_ref.casefold() == "%i0.1"), None)
    if direct is None or direct.semantic_state is not PLCSemanticState.FULL:
        raise RuntimeError(f"direct located address was not canonically bound: {direct}")
    if direct.canonical_display is None or direct.canonical_display.casefold() != "address::%i0.1":
        raise RuntimeError(f"unexpected direct located identity: {direct}")
    if profile["typed_boolean_theorem_gaps"] != 0:
        raise RuntimeError(f"valid Boolean identity was unexpectedly withheld: {profile}")

    return {
        "project_identity": profile["project_identity"],
        "canonical_symbols": profile["canonical_symbols"],
        "canonical_types": profile["canonical_types"],
        "ddt_types": profile["ddt_types"],
        "array_types": profile["array_types"],
        "io_identities": profile["io_identities"],
        "identity_contract": profile["identity_contract"],
        "direct_located_binding": direct.canonical_display,
    }


def _fail_closed_alias(root: Path) -> dict[str, object]:
    _write(root / "Alias.xst", _alias())
    result = run_production_verification_v5(root / "Alias.xst")
    profile = schneider_capability_profile_v8(result.engineering.project)
    if profile["physical_address_aliases"] < 1:
        raise RuntimeError("V8 did not identify referenced physical-address aliasing")
    if profile["identity_contract"] != "PARTIAL_FAIL_CLOSED":
        raise RuntimeError(f"V8 physical alias did not fail closed: {profile}")
    return {
        "physical_address_aliases": profile["physical_address_aliases"],
        "identity_contract": profile["identity_contract"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify Schneider V8 canonical symbol/type/I/O identity")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v8-") as directory:
        root = Path(directory)
        identity = root / "identity"
        identity.mkdir()
        alias = root / "alias"
        alias.mkdir()
        report = {
            "schema": "devagent-schneider-production-qualification-v8",
            "contract": (
                "Project-wide Schneider Control Expert canonical symbol/type/I/O identity over the V1-V7 theorem stack: "
                "DDT/ARRAY members, DFB instance/interface identities, located/topological addresses, exact source read/write "
                "bindings, whole/member ownership, and fail-closed unresolved/conflicting or physical-alias identity."
            ),
            "canonical_identity": _identity_bundle(identity),
            "fail_closed_physical_alias": _fail_closed_alias(alias),
            "runtime_boundary": {
                "dynamic_array_index_runtime_proof": False,
                "field_wiring_proven": False,
                "io_refresh_timing_proven": False,
                "forces_proven": False,
                "control_expert_simulator_executed": False,
                "hil_executed": False,
                "real_modicon_plc_executed": False,
            },
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
