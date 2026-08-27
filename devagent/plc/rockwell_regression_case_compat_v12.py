from __future__ import annotations

import re

from devagent.plc import rockwell_regression_evidence_hardening as _hardening

_INSTALLED = False


def _normalized_text(value: str) -> str:
    """Compare generated FAT expectations by semantic spelling, not tag case."""

    return re.sub(r"\s+", "", str(value or "")).casefold()


def _fat_plan_changes(baseline, engineering):
    """Report FAT-plan deltas without treating Rockwell identifier case as logic change.

    Rockwell identifiers are case-insensitive. The V12 engineer-ready procedure
    text preserves source spelling for readability, so a case-only PLC export
    must not create a false FAT_RECOMMENDATION_CHANGED regression.
    """

    before = {_hardening._fat_key(item): item for item in baseline.fat_tests}
    after = {_hardening._fat_key(item): item for item in engineering.fat_tests}
    changes = []
    for key in sorted(set(before) | set(after), key=repr):
        previous = before.get(key)
        current = after.get(key)
        if (
            previous is not None
            and current is not None
            and _normalized_text(previous.expected) == _normalized_text(current.expected)
        ):
            continue

        representative = current or previous
        assert representative is not None
        if previous is None:
            change_type = "FAT_RECOMMENDATION_ADDED"
            summary = (
                f"New FAT procedure {current.id} is recommended because the current "
                "PLC revision exposes new/changed behavior."
            )
            affected_test_ids = (current.id,)
        elif current is None:
            change_type = "FAT_RECOMMENDATION_REMOVED"
            summary = (
                f"Previous FAT procedure {previous.id} no longer maps to the current PLC revision; "
                "review whether its acceptance intent is still required."
            )
            affected_test_ids = ()
        else:
            change_type = "FAT_RECOMMENDATION_CHANGED"
            summary = (
                f"FAT procedure for {current.source.locator} changed; rerun the current procedure {current.id}."
            )
            affected_test_ids = (current.id,)

        changes.append(
            _hardening.RegressionChange(
                _hardening.stable_id("REG-FAT", repr(key)),
                change_type,
                representative.source.locator,
                summary,
                _hardening._affected(
                    (representative.output_tag,),
                    representative.preconditions.keys(),
                    representative.watch_tags,
                ),
                affected_test_ids=affected_test_ids,
                severity=_hardening.Severity.MEDIUM,
            )
        )
    return changes


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _hardening._fat_plan_changes = _fat_plan_changes
    _INSTALLED = True


__all__ = ["install"]
