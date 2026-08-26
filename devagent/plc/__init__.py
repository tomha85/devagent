"""Vendor-neutral PLC engineering foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x"]
