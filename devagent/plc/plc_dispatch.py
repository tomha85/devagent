from __future__ import annotations

from pathlib import Path

from devagent.plc.safe_analysis import analyze_rockwell_l5x
from devagent.plc.schneider_control_expert_v1 import (
    SchneiderInputError,
    analyze_schneider_control_expert,
    detect_schneider_input,
)
from devagent.plc.siemens_tia_v1 import SiemensInputError, analyze_siemens_tia, detect_siemens_input


def detect_plc_vendor(path: Path) -> str:
    target = path.expanduser().resolve(strict=False)
    if target.is_dir():
        siemens = detect_siemens_input(target)
        schneider = detect_schneider_input(target)
        if siemens and schneider:
            raise ValueError(
                "PLC export directory is ambiguous: it contains both Siemens TIA and Schneider Control Expert source surfaces. Analyze each vendor export in a separate directory."
            )
        if siemens:
            return "SIEMENS"
        if schneider:
            return "SCHNEIDER"
        raise ValueError(
            "PLC export directory was not recognized. Expected Siemens TIA exported sources or Schneider Control Expert .XEF/X* XML exchange exports."
        )

    suffix = target.suffix.lower()
    if suffix == ".l5x":
        return "ROCKWELL"
    if suffix.startswith((".ap", ".zap")):
        raise SiemensInputError(
            "TIA Portal .ap*/.zap* project archives are not parsed directly. Export PLC blocks/tag tables via TIA Portal Openness/XML or GenerateSource and pass that export bundle to DevAgent."
        )
    if suffix in {".stu", ".sta", ".zef"}:
        if suffix == ".zef":
            raise SchneiderInputError(
                "Schneider .ZEF is an export package/archive. Extract/export the contained .XEF with EcoStruxure Control Expert and pass the .XEF or granular X* exports to DevAgent."
            )
        raise SchneiderInputError(
            f"Schneider {suffix.upper()} is a Control Expert work/archive format. Export the project to .XEF before analysis."
        )
    if detect_siemens_input(target):
        return "SIEMENS"
    if detect_schneider_input(target):
        return "SCHNEIDER"
    raise ValueError(
        f"PLC engineering artifact is not recognized: {target}. Supported inputs are Rockwell full-project .L5X, Siemens TIA Openness/XML/generated-source exports, and Schneider Control Expert .XEF/X* XML exchange exports."
    )


def analyze_plc_project(path: Path):
    vendor = detect_plc_vendor(path)
    if vendor == "ROCKWELL":
        return analyze_rockwell_l5x(path)
    if vendor == "SIEMENS":
        return analyze_siemens_tia(path)
    if vendor == "SCHNEIDER":
        return analyze_schneider_control_expert(path)
    raise AssertionError(vendor)


__all__ = ["analyze_plc_project", "detect_plc_vendor"]
