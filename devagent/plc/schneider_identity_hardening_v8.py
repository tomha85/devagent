from __future__ import annotations

from devagent.plc import schneider_identity_types_v8 as _v8


_INSTALLED = False
_PREVIOUS_BUILD_SYMBOLS = _v8._build_symbols


def _hardened_build_symbols(project, types, files):
    """Exclude DDT/DFB interface `<variables>` that V1 inventories as flat tags.

    Control Expert reuses the `<variables>` element for controller variables,
    DDT members and DFB interface/local variables. The V1 inventory intentionally
    keeps that broad surface for backward compatibility. V8 owns canonical scope,
    so only variables discovered outside DDT/FBSource containers may become
    controller-root identities. DDT members and DFB parameters are reintroduced
    through their exact type/instance scopes by the V8 identity builder.
    """
    raw_globals, _raw_types, _conflicts = _v8._raw_inventory(files)
    original_tags = project.tags
    try:
        project.tags = [
            tag for tag in original_tags
            if tag.name.casefold() in raw_globals
        ]
        return _PREVIOUS_BUILD_SYMBOLS(project, types, files)
    finally:
        project.tags = original_tags


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _v8._build_symbols = _hardened_build_symbols
    _INSTALLED = True


__all__ = ["install"]
