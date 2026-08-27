from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Clarify the existing PLC CLI input contract after production is loaded."""
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import cli as _cli

    previous = _cli._parser

    def _parser():
        parser = previous()
        parser.description = (
            "Evidence-driven PLC engineering review, requirements verification, logic/risk analysis, regression impact analysis, "
            "and engineer-ready FAT planning for qualified Rockwell Studio 5000 L5X and Siemens TIA Portal engineering exports. "
            "DevAgent does not connect to or execute external PLC software."
        )
        for action in parser._actions:
            if getattr(action, "dest", None) == "project":
                action.help = (
                    "Rockwell Studio 5000 full-project .L5X, or Siemens TIA Portal Openness/XML/generated-source file or export directory "
                    "(.scl/.db/.udt/.xml/.stl/.awl). Proprietary .ap*/.zap* archives must be exported first."
                )
            elif getattr(action, "dest", None) == "baseline":
                action.help = (
                    "Previous same-vendor PLC engineering artifact/export bundle for revision impact, affected requirements, and FAT retest analysis"
                )
        return parser

    _cli._parser = _parser
    _INSTALLED = True


__all__ = ["install"]
