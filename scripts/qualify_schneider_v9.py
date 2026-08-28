from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.schneider_closeout_v9 import schneider_capability_profile_v9


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _xst(name: str = "Main") -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9Qualification" version="1.0" />
  <program>
    <identProgram name="{name}" type="section" task="MAST" />
    <STSource>
Run := Start;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" topologicalAddress="%I0.0" />
    <variables name="Run" typeName="BOOL" topologicalAddress="%Q0.0" />
  </dataBlock>
</STExchangeFile>
'''


def _sfc() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<SFCExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SchneiderV9Qualification" version="1.0" />
  <program>
    <identProgram name="Sequence" type="section" task="MAST" />
    <SFCSource><step name="S0" /></SFCSource>
  </program>
</SFCExchangeFile>
'''


def _qualify_full(root: Path) -> dict[str, object]:
    _write(root / "Main.xst", _xst())
    production = run_production_verification_v5(root / "Main.xst")
    profile = schneider_capability_profile_v9(production.engineering.project)

    if profile["schema"] != "devagent-schneider-control-expert-capability-v9":
        raise RuntimeError(f"unexpected V9 schema: {profile}")
    if not profile["coverage_accounting_complete"]:
        raise RuntimeError(f"V9 source accounting incomplete: {profile}")
    if profile["support_contract"] != "FULL":
        raise RuntimeError(f"simple bounded source did not close FULL: {profile}")
    if profile["source_files"] != 1:
        raise RuntimeError(f"unexpected source manifest: {profile}")
    if len(profile["deterministic_manifest_sha256"]) != 64:
        raise RuntimeError(f"invalid deterministic manifest: {profile}")
    if profile["real_control_expert_export_corpus"] != "PENDING_EXTERNAL_EVIDENCE":
        raise RuntimeError(f"external corpus gate was incorrectly promoted: {profile}")
    if profile["simulator_hil_real_plc_execution"] != "NOT_EXECUTED":
        raise RuntimeError(f"runtime gate was incorrectly promoted: {profile}")

    return {
        "support_contract": profile["support_contract"],
        "support_regions": profile["support_regions"],
        "coverage_accounting_complete": profile["coverage_accounting_complete"],
        "source_files": profile["source_files"],
        "source_bytes": profile["source_bytes"],
        "manifest_sha256": profile["deterministic_manifest_sha256"],
    }


def _qualify_fail_closed(root: Path) -> dict[str, object]:
    _write(root / "Sequence.xsf", _sfc())
    production = run_production_verification_v5(root / "Sequence.xsf")
    profile = schneider_capability_profile_v9(production.engineering.project)

    if profile["support_contract"] != "PARTIAL_FAIL_CLOSED":
        raise RuntimeError(f"opaque SFC did not fail closed: {profile}")
    if profile["support_opaque"] < 1:
        raise RuntimeError(f"opaque SFC missing from V9 support contract: {profile}")

    return {
        "support_contract": profile["support_contract"],
        "support_opaque": profile["support_opaque"],
        "support_partial": profile["support_partial"],
        "support_protected": profile["support_protected"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify Schneider V9 commercial/source-support closeout")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-schneider-v9-") as directory:
        root = Path(directory)
        full = root / "full"
        full.mkdir()
        gaps = root / "gaps"
        gaps.mkdir()
        report = {
            "schema": "devagent-schneider-production-qualification-v9",
            "contract": (
                "Explicit Control Expert source-support accounting and deterministic export-bundle identity over the V1-V8 "
                "theorem stack. Every imported executable statement/source region/protected DFB/call boundary receives an "
                "explicit support disposition; non-FULL regions fail closed."
            ),
            "bounded_full_case": _qualify_full(full),
            "fail_closed_opaque_case": _qualify_fail_closed(gaps),
            "external_evidence_gates": {
                "real_control_expert_export_corpus": "PENDING_EXTERNAL_EVIDENCE",
                "control_expert_simulator": "NOT_EXECUTED",
                "hil": "NOT_EXECUTED",
                "real_modicon_plc": "NOT_EXECUTED",
            },
            "commercial_claim_boundary": (
                "Implementation-qualified static source closeout only; no customer-export corpus or runtime PLC execution is claimed."
            ),
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
