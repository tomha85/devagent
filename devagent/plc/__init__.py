"""Vendor-neutral PLC engineering and production verification foundations for DevAgent."""

from devagent.plc import analysis as _analysis
from devagent.plc.rockwell_compare_hardening import install as _install_rockwell_compare_hardening
from devagent.plc.rockwell_alias_hardening import install as _install_rockwell_alias_hardening
from devagent.plc.rockwell_entrypoint_hardening import install as _install_rockwell_entrypoint_hardening
from devagent.plc.rockwell_st_v10 import install as _install_rockwell_st_v10

# Install fail-closed compare, alias, controller-entrypoint, and ST guards before
# production verification is imported. Every downstream Rockwell proof must
# share the same canonical writer identity and executable-entrypoint model.
_install_rockwell_compare_hardening()
_install_rockwell_alias_hardening()
_install_rockwell_entrypoint_hardening()
_install_rockwell_st_v10()

from devagent.plc.rockwell_compare_reachability_hardening import (
    install as _install_rockwell_compare_reachability_hardening,
)

# The typed-compare theorem/FAT path is independent from boolean output-logic
# normalization, so explicitly bind it to the same executable routine closure.
_install_rockwell_compare_reachability_hardening()

from devagent.plc.rockwell_requirement_hardening import install as _install_rockwell_requirement_hardening
from devagent.plc.rockwell_v10_semantics import install as _install_rockwell_v10_semantics
from devagent.plc.rockwell_action_requirements import install as _install_rockwell_action_requirements
from devagent.plc.rockwell_risk_hardening import install as _install_rockwell_risk_hardening
from devagent.plc.rockwell_closeout_gate_hardening import install as _install_rockwell_closeout_gate_hardening

_install_rockwell_requirement_hardening()
# V10 extends the already-hardened V9 theorem. It proves bounded OTL/OTU action
# effects and may prove final retained scan state only inside the deliberately
# narrow same-active-Main-RLL-routine ordering theorem. Wider scheduling remains
# fail-closed until explicitly modeled.
_install_rockwell_v10_semantics()
# Explicit natural-language requirements may bind to deterministic MOV/COPY/
# CLR/RES local action effects, but never to final scan/process behavior.
_install_rockwell_action_requirements()
_install_rockwell_risk_hardening()
# Install the V9 support-contract guard before importing regression/production
# modules. Those modules import safe_analysis by value, so the patched support
# check must already be visible when safe_analysis is first loaded.
_install_rockwell_closeout_gate_hardening()

from devagent.plc.rockwell_core_review_v12 import install as _install_rockwell_core_review_v12

# V12 makes the commercial engineering-review contract explicit: cause/effect,
# unreachable logic, contradictory linear paths, and sequence-branch risks are
# deterministic review outputs. They remain evidence-backed findings, not AI
# guesses and not runtime PASS claims.
_install_rockwell_core_review_v12()

from devagent.plc.fat_procedure_v12 import install as _install_fat_procedure_v12

# Every FAT candidate, including tests created later by requirement mapping,
# must be an engineer-ready manual procedure. DevAgent plans the FAT; it does not
# connect to or execute the engineer's external simulator/HIL/controller.
_install_fat_procedure_v12()

from devagent.plc.rockwell_regression_evidence_hardening import (
    install as _install_rockwell_regression_evidence_hardening,
    install_domain_evidence as _install_rockwell_regression_domain_evidence,
)

_install_rockwell_regression_evidence_hardening()

from devagent.plc.rockwell_regression_case_compat_v12 import (
    install as _install_rockwell_regression_case_compat_v12,
)

# FAT-plan regression is semantic and case-insensitive. Studio 5000 identifier
# spelling changes alone must not force a retest or create a false regression.
_install_rockwell_regression_case_compat_v12()

from devagent.plc.rockwell_branch_coverage_v16 import install as _install_rockwell_branch_coverage_v16

# V16 reconciles branch coverage with the full deterministic theorem set. A
# neutral-text branch proven by the bounded data/compute action theorem counts
# as modeled even when it is not an OTE/OTL/OTU boolean-output branch. Mixed or
# partially understood branch grammars remain fail-closed and withheld.
_install_rockwell_branch_coverage_v16()

from devagent.plc.semantic_coverage_report import install as _install_semantic_coverage_report

# Ensure any later CLI import of render_production_report receives the
# reachability-aware semantic coverage augmentation.
_install_semantic_coverage_report()

from devagent.plc.four_contract_v13 import install as _install_four_contract_v13

# V13 makes the commercial four-core contract explicit and testable:
# engineering analysis, logic/risk review, five-area optimization recommendations,
# and engineer-ready FAT planning. All optimization output is advisory only.
_install_four_contract_v13()

from devagent.plc.professional_report_install_v14 import install as _install_professional_report_v14

# V14 adds a customer-facing executive layer while preserving every detailed
# engineering/evidence section and the established public renderer identity.
# This is presentation-only: it cannot change deterministic verdicts/readiness.
_install_professional_report_v14()

from devagent.plc.report_clarity_install_v16 import install as _install_report_clarity_v16

# V16 clarifies project-only review and coverage terminology without changing
# deterministic engineering, risk, FAT, or release-readiness decisions.
_install_report_clarity_v16()

from devagent.plc.agent_harness_install_v15 import install as _install_agent_harness_v15

# V15 applies modern agent orchestration only to the probabilistic assistance
# layer: bounded propose/critic/revise graphs, deterministic evidence guards, and
# per-run trace capture. The PLC proof/risk/readiness core stays authoritative.
_install_agent_harness_v15()

from devagent.plc.production_v5 import run_production_verification_v5
# Production is now loaded; bind the stage-14 baseline evidence augmentation.
_install_rockwell_regression_domain_evidence()
from devagent.plc.safe_analysis import analyze_rockwell_l5x

# Keep existing imports from ``devagent.plc.analysis`` on the guarded public path.
# The base module remains reusable by ``safe_analysis`` without recursive calls.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_rockwell_l5x", "run_production_verification_v5"]
