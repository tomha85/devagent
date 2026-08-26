"""Vendor-neutral PLC engineering and production verification foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.rockwell_compare_hardening import install as _install_rockwell_compare_hardening
from devagent.plc.rockwell_alias_hardening import install as _install_rockwell_alias_hardening
from devagent.plc.rockwell_entrypoint_hardening import install as _install_rockwell_entrypoint_hardening

# Install fail-closed compare, alias, and controller-entrypoint guards before
# production verification is imported. Every downstream Rockwell proof must
# share the same canonical writer identity and executable-entrypoint model.
_install_rockwell_compare_hardening()
_install_rockwell_alias_hardening()
_install_rockwell_entrypoint_hardening()

from devagent.plc.rockwell_compare_reachability_hardening import (
    install as _install_rockwell_compare_reachability_hardening,
)

# The typed-compare theorem/FAT path is independent from boolean output-logic
# normalization, so explicitly bind it to the same executable routine closure.
_install_rockwell_compare_reachability_hardening()

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

from devagent.plc import production_v5 as _production_v5
from devagent.plc.project_test_plan_hardening import install as _install_project_test_plan_hardening

# Production is now loaded; bind stage-14 baseline evidence and V10 generic
# project-specific test planning. The V10 planner consumes normalized semantics
# only and deliberately has no equipment/domain-name rules.
_install_rockwell_regression_domain_evidence()
_install_project_test_plan_hardening()
run_production_verification_v5 = _production_v5.run_production_verification_v5

from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x", "run_production_verification_v5"]
