"""Vendor-neutral PLC engineering and production verification foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x", "run_production_verification_v5"]
