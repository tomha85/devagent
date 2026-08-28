from __future__ import annotations

from devagent.plc import semantic_coverage_report as _semantic_report
from devagent.plc.professional_report_v14 import render_professional_overview

_INSTALLED = False


def install() -> None:
    """Add the professional executive layer without changing renderer identity.

    Existing compatibility tests intentionally require
    ``production_report.render_production_report`` to remain the semantic coverage
    renderer function. V14 therefore wraps only that renderer's captured base
    report function instead of replacing the public function object.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    original_base_render = _semantic_report._ORIGINAL_RENDER

    def render_base_with_professional_overview(result):
        base = original_base_render(result)
        overview = render_professional_overview(result)
        marker = "## Executive Summary"
        if marker in base:
            # Keep the original run-identity bullets, but move them under the
            # Technical Verification Identity heading emitted by the overview.
            return base.replace(marker, overview, 1)
        return overview + "\n" + base

    _semantic_report._ORIGINAL_RENDER = render_base_with_professional_overview

    # Level 1 terminal reporting is a separate presentation surface. Install the
    # engineer-focused Risk -> Why -> Impact -> Recommended Action -> Fix/FAT
    # summary before ``devagent.plc.cli`` imports ``render_console_summary``.
    # This does not alter the complete report, risk register, proof, FAT, or
    # readiness decisions.
    from devagent.plc.top_engineering_risks_v1 import install as _install_top_engineering_risks_v1
    from devagent.plc.report_contract_console_v1 import install as _install_report_contract_console_v1

    _install_top_engineering_risks_v1()
    # Install after the risk-summary wrapper so the final Level 1 surface shows
    # both root-cause risk grouping and the independent analysis/proof/FAT/release
    # decision separation contract.
    _install_report_contract_console_v1()
    _INSTALLED = True


__all__ = ["install"]
