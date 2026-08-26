"""Vendor-neutral PLC engineering and production verification foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.rockwell_compare_hardening import install as _install_rockwell_compare_hardening

# Install V8 fail-closed compare guards before production_verification imports
# the typed requirement theorem. This keeps public, FAT, graph, and production
# paths on one guarded implementation.
_install_rockwell_compare_hardening()

from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x", "run_production_verification_v5"]
