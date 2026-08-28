from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Merge duplicate Schneider variable metadata without changing tag identity.

    Granular export directories may contain the same variable in an .XST section
    dataBlock and later in .XSY with its located/topological address. Preserve the
    canonical identity while allowing the richer address-bearing record to win.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import schneider_control_expert_v1 as _schneider
    from devagent.plc.models import PLCTag

    def _dedupe_tag(state, tag: PLCTag) -> None:
        key = (tag.scope.casefold(), tag.name.casefold())
        for index, current in enumerate(state.tags):
            if (current.scope.casefold(), current.name.casefold()) != key:
                continue
            current_has_address = "control expert address " in (current.description or "").casefold()
            incoming_has_address = "control expert address " in (tag.description or "").casefold()
            richer_type = current.data_type == "UNKNOWN" and tag.data_type != "UNKNOWN"
            if incoming_has_address and not current_has_address or richer_type:
                state.tags[index] = PLCTag(
                    id=current.id,
                    name=current.name,
                    scope=current.scope,
                    data_type=tag.data_type if richer_type or tag.data_type != "UNKNOWN" else current.data_type,
                    tag_type=tag.tag_type or current.tag_type,
                    alias_for=current.alias_for,
                    external_access=current.external_access,
                    constant=current.constant,
                    description=tag.description or current.description,
                )
            return
        state.tags.append(tag)

    _schneider._dedupe_tag = _dedupe_tag
    _INSTALLED = True


__all__ = ["install"]
