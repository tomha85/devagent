from __future__ import annotations

from pathlib import Path

from devagent.plc.safe_analysis import analyze_rockwell_l5x
from devagent.plc.siemens_tia_v1 import SiemensInputError, analyze_siemens_tia, detect_siemens_input


def detect_plc_vendor(path: Path) -> str:
    target = path.expanduser().resolve(strict=False)
    if target.is_dir():
        if detect_siemens_input(target):
            return "SIEMENS"
        raise ValueError(
            "PLC export directory was not recognized. Siemens directories must contain TIA Openness/XML or generated source files (.scl/.db/.udt/.stl/.awl)."
        )

    suffix = target.suffix.lower()
    if suffix == ".l5x":
        return "ROCKWELL"
    if suffix.startswith((".ap", ".zap")):
        raise SiemensInputError(
            "TIA Portal .ap*/.zap* project archives are not parsed directly. Export PLC blocks/tag tables via TIA Portal Openness/XML or GenerateSource and pass that export bundle to DevAgent."
        )
    if detect_siemens_input(target):
        return "SIEMENS"
    raise ValueError(
        f"PLC engineering artifact is not recognized: {target}. Supported inputs are Rockwell full-project .L5X and Siemens TIA Openness/XML/generated-source exports."
    )


def analyze_plc_project(path: Path):
    vendor = detect_plc_vendor(path)
    if vendor == "ROCKWELL":
        return analyze_rockwell_l5x(path)
    if vendor == "SIEMENS":
        return analyze_siemens_tia(path)
    raise AssertionError(vendor)


__all__ = ["analyze_plc_project", "detect_plc_vendor"]
