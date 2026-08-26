"""Vendor-neutral PLC engineering and production verification foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.rockwell_compare_hardening import install as _install_rockwell_compare_hardening
from devagent.plc.rockwell_alias_hardening import install as _install_rockwell_alias_hardening

# Install fail-closed compare and alias guards before production verification is
# imported. Every downstream Rockwell proof must share the same canonical tag
# and writer identity.
_install_rockwell_compare_hardening()
_install_rockwell_alias_hardening()

from devagent.plc.rockwell_requirement_hardening import install as _install_rockwell_requirement_hardening
from devagent.plc.rockwell_risk_hardening import install as _install_rockwell_risk_hardening
from devagent.plc.rockwell_closeout_gate_hardening import install as _install_rockwell_closeout_gate_hardening

_install_rockwell_requirement_hardening()
_install_rockwell_risk_hardening()
# Install the V9 support-contract guard before importing regression/production
# modules. Those modules import safe_analysis by value, so the patched support
# check must already be visible when safe_analysis is first loaded.
_install_rockwell_closeout_gate_hardening()

from devagent.plc.rockwell_regression_evidence_hardening import (
    install as _install_rockwell_regression_evidence_hardening,
    install_domain_evidence as _install_rockwell_regression_domain_evidence,
)

_install_rockwell_regression_evidence_hardening()

from devagent.plc.production_v5 import run_production_verification_v5
# Production is now loaded; bind the stage-14 baseline evidence augmentation.
_install_rockwell_regression_domain_evidence()
from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x", "run_production_verification_v5"]
