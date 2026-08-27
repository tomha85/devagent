from __future__ import annotations

from dataclasses import replace
import re

from devagent.plc import siemens_scl_control_flow_v2 as _v2


_INSTALLED = False
_PREVIOUS_UPGRADE = _v2._upgrade_if_chains
_CONTROL_OPEN = re.compile(r"^\s*(CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_CONTROL_CLOSE = re.compile(r"^\s*(END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b", re.IGNORECASE)
_WITHHELD = "__DEVAGENT_SIEMENS_V2_NESTED_CONTROL_WITHHELD__"


def _nested_if_ids(project) -> set[str]:
    """Return IF statements that are not at top-level executable depth.

    Siemens V2 deliberately models only complete single-level IF chains. A
    syntactically complete inner IF must not become independently FULL when its
    enclosing control region is still PARTIAL/OPAQUE.
    """

    nested: set[str] = set()
    for statements in _v2._group_scl_statements(project).values():
        depth = 0
        for statement in statements:
            text = statement.text.strip()
            if _v2._END_IF.match(text) or _CONTROL_CLOSE.match(text):
                depth = max(0, depth - 1)
                continue
            if _v2._IF.match(text):
                if depth > 0:
                    nested.add(statement.id)
                depth += 1
                continue
            if _CONTROL_OPEN.match(text):
                depth += 1
    return nested


def _upgrade_top_level_only(project) -> int:
    nested = _nested_if_ids(project)
    if not nested:
        return _PREVIOUS_UPGRADE(project)

    originals = {item.id: item for item in project.logic_statements if item.id in nested}
    project.logic_statements = [
        replace(item, text=_WITHHELD) if item.id in nested else item
        for item in project.logic_statements
    ]
    try:
        modeled = _PREVIOUS_UPGRADE(project)
    finally:
        # Preserve any semantic-state change made outside the withheld nested IF
        # statement itself, but always restore the exact source text/evidence.
        project.logic_statements = [
            replace(item, text=originals[item.id].text)
            if item.id in originals
            else item
            for item in project.logic_statements
        ]
    return modeled


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _v2._upgrade_if_chains = _upgrade_top_level_only
    _INSTALLED = True

    # V3 must capture the already-hardened V2 analyzer. Importing here, rather
    # than at module import time, preserves the install order while keeping the
    # Siemens call-graph extension isolated from the qualified Rockwell path.
    from devagent.plc.siemens_call_graph_v3 import install as _install_siemens_call_graph_v3

    _install_siemens_call_graph_v3()

    # V4 runs after the V3 call/interface closure. It may upgrade only bounded
    # LAD/FBD FlgNet networks, then rebuilds V3 projection so local FB/FC visual
    # logic cannot prove active machine behavior without the same OB/call guards.
    from devagent.plc.siemens_flgnet_v4 import install as _install_siemens_flgnet_v4

    _install_siemens_flgnet_v4()


__all__ = ["install"]
