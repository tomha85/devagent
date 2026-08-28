from __future__ import annotations

from contextvars import ContextVar
import re

_INSTALLED = False
_ACTIVE_VENDOR: ContextVar[str | None] = ContextVar("devagent_plc_cli_active_schneider_vendor", default=None)


def _schneider_progress_summary(stage) -> str:
    summary = stage.summary
    if stage.number == 1 and summary.startswith("Validated Rockwell full-project L5X for "):
        controller = summary.removeprefix("Validated Rockwell full-project L5X for ").removesuffix(".")
        return (
            f"Validated Schneider EcoStruxure Control Expert XML exchange export for {controller}; "
            "work/archive opening and simulator execution are outside DevAgent."
        )
    if stage.number == 2:
        match = re.fullmatch(
            r"Canonical IR: (\d+) tags, (\d+) routines, (\d+) RLL rungs, (\d+) ST statements\.",
            summary,
        )
        if match:
            tags, routines, _rungs, statements = match.groups()
            return (
                f"Canonical IR: {tags} variables, {routines} sections/routines, {statements} ST statement(s); "
                "LD/FBD/SFC/IL coverage is finalized in the Schneider vendor result."
            )
    return summary


def install() -> None:
    """Expose Schneider Control Expert input/help and vendor-correct live progress."""
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import cli as _cli
    from devagent.plc.plc_dispatch import detect_plc_vendor
    from devagent.plc.schneider_report_install_v1 import install as _install_schneider_report

    # Report dispatch is presentation-only and is installed from the same final
    # package hook so library and CLI consumers receive Schneider-correct
    # semantic-coverage wording without changing Rockwell/Siemens renderers.
    _install_schneider_report()

    previous_parser = _cli._parser
    previous_header = _cli._print_run_header
    previous_progress_call = _cli._PLCProgressStatus.__call__

    def _parser():
        parser = previous_parser()
        parser.description = (
            "Evidence-driven PLC engineering review, requirements verification, logic/risk analysis, regression impact analysis, "
            "and engineer-ready FAT planning for qualified Rockwell Studio 5000 L5X, Siemens TIA Portal exports, and Schneider EcoStruxure Control Expert XML exchange exports. "
            "DevAgent does not connect to or execute external PLC software."
        )
        for action in parser._actions:
            if getattr(action, "dest", None) == "project":
                action.help = (
                    "Rockwell full-project .L5X; Siemens TIA Openness/XML/generated sources (.scl/.db/.udt/.xml/.stl/.awl); "
                    "or Schneider Control Expert .XEF/granular XML exchange exports (.XSY/.XST/.XLD/.XBD/.XSF/.XIL/.XDD/.XDB/.XHW/.XCM). "
                    "Siemens .ap*/.zap* and Schneider .STU/.STA/.ZEF containers must be exported/extracted to supported engineering sources first."
                )
        return parser

    def _print_run_header(args, provider_name, model_name):
        try:
            vendor = detect_plc_vendor(args.project)
        except Exception:
            vendor = None
        _ACTIVE_VENDOR.set(vendor)
        return previous_header(args, provider_name, model_name)

    def _progress_call(self, stage):
        if (
            _ACTIVE_VENDOR.get() == "SCHNEIDER"
            and self.verbose
            and stage.number not in self._completed
            and stage.number in {1, 2}
        ):
            if stage.number > self._active_stage:
                self._show_stage(stage.number)
            self._completed.add(stage.number)
            self.sink(f"      -> {stage.status.value}: {_schneider_progress_summary(stage)}")
            if stage.number == self._active_stage:
                self._show_stage(stage.number + 1)
            return
        return previous_progress_call(self, stage)

    _cli._parser = _parser
    _cli._print_run_header = _print_run_header
    _cli._PLCProgressStatus.__call__ = _progress_call
    _INSTALLED = True


__all__ = ["install"]
