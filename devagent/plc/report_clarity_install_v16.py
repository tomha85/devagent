from __future__ import annotations

from devagent.plc import semantic_coverage_report as _semantic_report

_INSTALLED = False


def install() -> None:
    """Improve customer-facing wording without changing any engineering verdict.

    V16 keeps the public renderer identity stable. It wraps only the semantic
    renderer's captured base report, after the V14 professional overview has
    already been installed. The deterministic proof, risk, FAT, and readiness
    engines remain unchanged.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    previous_base_render = _semantic_report._ORIGINAL_RENDER

    def render_base_with_v16_clarity(result):
        base = previous_base_render(result)

        # The legacy project-level counter means the instruction mnemonic had a
        # directional read/write model. It is not occurrence-level behavior
        # proof, which is reported separately in Semantic Coverage.
        base = base.replace(
            "- Instruction semantic coverage:",
            "- Directional instruction recognition coverage:",
        )
        base = base.replace(
            "- Branch-path coverage:",
            "- Bounded branch semantic coverage:",
        )

        if not result.requirements:
            scope = (
                "> **Review mode:** **PROJECT_ONLY_ENGINEERING_REVIEW**\n"
                "> **Customer requirements:** **NOT SUPPLIED**\n"
                "> **Requirement compliance:** **NOT EVALUATED — no customer/engineering requirement artifact was supplied.**\n"
                "> **Project engineering review:** **COMPLETED within the declared semantic/proof boundary.**\n"
            )
            marker = "> **Engineering outcome:**"
            if marker in base and "PROJECT_ONLY_ENGINEERING_REVIEW" not in base:
                base = base.replace(marker, scope + ">\n" + marker, 1)

            base = base.replace(
                "0 proven / 0 unresolved of 0 mapped evaluations",
                "NOT EVALUATED — requirements not supplied",
                1,
            )
            base = base.replace(
                "Unresolved or conflicting requirements remain visible and are not promoted to PASS.",
                "Requirement compliance is outside this project-only review because no requirement artifact was supplied; DevAgent makes no customer-specification compliance claim.",
                1,
            )

        return base

    _semantic_report._ORIGINAL_RENDER = render_base_with_v16_clarity
    _INSTALLED = True


__all__ = ["install"]
