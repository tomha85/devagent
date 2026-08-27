from __future__ import annotations

from contextvars import ContextVar
import re

_INSTALLED = False
_ACTIVE_VENDOR: ContextVar[str | None] = ContextVar("devagent_plc_cli_active_vendor", default=None)


def _siemens_progress_summary(stage) -> str:
    summary = stage.summary
    if stage.number == 1 and summary.startswith("Validated Rockwell full-project L5X for "):
        controller = summary.removeprefix("Validated Rockwell full-project L5X for ").removesuffix(".")
        return (
            f"Validated Siemens TIA Portal engineering export bundle for {controller}; "
            "proprietary TIA project execution/opening is outside DevAgent."
        )
    if stage.number == 2:
        match = re.fullmatch(
            r"Canonical IR: (\d+) tags, (\d+) routines, (\d+) RLL rungs, (\d+) ST statements\.",
            summary,
        )
        if match:
            tags, routines, _rungs, statements = match.groups()
            return (
                f"Canonical IR: {tags} tags/symbols, {routines} routines/blocks, "
                f"{statements} SCL statements."
            )
    return summary


def install() -> None:
    """Clarify Siemens CLI input and live-progress presentation after production loads."""
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import cli as _cli
    from devagent.plc.plc_dispatch import detect_plc_vendor

    previous_parser = _cli._parser
    previous_header = _cli._print_run_header
    previous_progress_call = _cli._PLCProgressStatus.__call__

    def _parser():
        parser = previous_parser()
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

    def _print_run_header(args, provider_name, model_name):
        try:
            vendor = detect_plc_vendor(args.project)
        except Exception:
            # Preserve the established analyzer/error path. Vendor detection here
            # is presentation-only and must never introduce a new CLI failure.
            vendor = None
        _ACTIVE_VENDOR.set(vendor)
        return previous_header(args, provider_name, model_name)

    def _progress_call(self, stage):
        # Finalized Siemens StageRecords are already vendor-correct. Only the
        # first V4 live summaries need presentation normalization because the
        # vendor-neutral production core historically emitted Rockwell wording
        # before the Siemens wrapper finalized those records.
        if (
            _ACTIVE_VENDOR.get() == "SIEMENS"
            and self.verbose
            and stage.number not in self._completed
            and stage.number in {1, 2}
        ):
            if stage.number > self._active_stage:
                self._show_stage(stage.number)
            self._completed.add(stage.number)
            self.sink(f"      -> {stage.status.value}: {_siemens_progress_summary(stage)}")
            if stage.number == self._active_stage:
                self._show_stage(stage.number + 1)
            return
        return previous_progress_call(self, stage)

    _cli._parser = _parser
    _cli._print_run_header = _print_run_header
    _cli._PLCProgressStatus.__call__ = _progress_call
    _INSTALLED = True


__all__ = ["install"]
