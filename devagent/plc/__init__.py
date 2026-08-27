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

from devagent.plc.rockwell_system_service_install_v17 import (
    install as _install_rockwell_system_service_v17,
)

# V17 does not widen static proof. Reachable GSV/SSV system-service logic stays
# PARTIAL, but every such runtime-dependent gap receives an evidence-linked,
# engineer-executed FAT procedure and a specific commissioning risk. The install
# happens before production imports capture analysis/risk functions by value.
_install_rockwell_system_service_v17()

from devagent.plc.semantic_coverage_report import install as _install_semantic_coverage_report

# Ensure any later CLI import of render_production_report receives the
# reachability-aware semantic coverage augmentation.
_install_semantic_coverage_report()

from devagent.plc.rockwell_system_service_report_install_v17 import (
    install as _install_rockwell_system_service_report_v17,
)

# Keep the public report renderer identity stable while adding an explicit V17
# system-service runtime boundary inside the semantic coverage section.
_install_rockwell_system_service_report_v17()

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

from devagent.plc.siemens_integration_v1 import install as _install_siemens_integration_v1

# Siemens V1 is a vendor branch over the already-qualified production contract.
# It accepts offline TIA Portal Openness/XML and generated-source exports, proves
# only bounded top-level SCL semantics, and leaves control-flow/LAD/FBD/GRAPH/STL
# or protected behavior PARTIAL/OPAQUE. Qualified Rockwell dispatch still enters
# the exact guarded Rockwell analyzer above.
_install_siemens_integration_v1()

from devagent.plc.siemens_scl_control_flow_v2 import install as _install_siemens_scl_control_flow_v2

# Siemens V2 adds a deliberately bounded deterministic theorem for complete,
# single-level IF/ELSIF/ELSE Boolean assignment chains. Missing/incomplete branch
# assignments, nesting, CASE/loops, calls, cyclic/self-references, and unsupported
# expressions stay fail-closed and require engineer-executed FAT evidence.
_install_siemens_scl_control_flow_v2()

from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.siemens_v5_bindings_v1 import install as _install_siemens_v5_bindings_v1

# Agent Harness V15 loads Production V5 before Siemens integration. Refresh only
# V5's by-value shared production/evidence/review bindings after Siemens installs.
# Siemens then reaches the vendor dispatcher while Rockwell remains behind the
# exact qualified Rockwell functions selected by those vendor-aware wrappers.
_install_siemens_v5_bindings_v1()

# Production is now loaded; bind the stage-14 baseline evidence augmentation.
_install_rockwell_regression_domain_evidence()
from devagent.plc.safe_analysis import analyze_rockwell_l5x
from devagent.plc.plc_dispatch import analyze_plc_project
from devagent.plc.siemens_cli_install_v1 import install as _install_siemens_cli_v1

# CLI parsing is loaded only after production_v5 exists, avoiding circular
# imports while exposing both vendor input contracts through `devagent plc`.
_install_siemens_cli_v1()

# Keep existing imports from ``devagent.plc.analysis`` on the guarded Rockwell
# public path. The base module remains reusable by ``safe_analysis`` without
# recursive calls; vendor-neutral callers should use analyze_plc_project.
_analysis.analyze_rockwell_l5x = analyze_rockwell_l5x

__all__ = ["analyze_plc_project", "analyze_rockwell_l5x", "run_production_verification_v5"]
