from __future__ import annotations

import re
from dataclasses import replace

from devagent.plc import rockwell_compare as _base
from devagent.plc import rockwell_compare_hardening as _hard

_PREVIOUS_COMPARE_MODELS = _hard.compare_models
_PROGRAM_QUALIFIED = re.compile(r"^Program:([^\.]+)\.(.+)$", re.IGNORECASE)
_INVALID_IDENTITY_SCOPES = {"alias-cycle", "unresolved-alias"}


def _program_from_scope(scope: str) -> str | None:
    prefix = "program:"
    return scope[len(prefix) :] if scope.casefold().startswith(prefix) else None


def _split_reference(ref: str) -> tuple[str, str]:
    top = re.split(r"[.\[]", ref, maxsplit=1)[0]
    return top, ref[len(top) :]


def _find_tag(project, ref: str, default_program: str | None):
    value = ref.strip()
    qualified = _PROGRAM_QUALIFIED.match(value)
    explicit_program = None
    if qualified is not None:
        explicit_program, value = qualified.groups()

    top, suffix = _split_reference(value)
    top_folded = top.casefold()
    scopes: list[str] = []
    if explicit_program:
        scopes.append(f"program:{explicit_program}".casefold())
    elif default_program:
        scopes.append(f"program:{default_program}".casefold())
        scopes.append("controller")
    else:
        scopes.append("controller")

    for scope in scopes:
        matches = [
            tag
            for tag in project.tags
            if tag.scope.casefold() == scope and tag.name.casefold() == top_folded
        ]
        if len(matches) == 1:
            return matches[0], scope, suffix

    fallback_scope = scopes[0] if scopes else "unresolved"
    return None, fallback_scope, suffix


def canonical_tag_identity(
    project,
    ref: str,
    default_program: str | None,
    *,
    _seen: frozenset[tuple[str, str]] = frozenset(),
    _resolving_alias: bool = False,
) -> tuple[str, str]:
    """Resolve scope, case, member suffixes, and Rockwell AliasFor chains.

    A missing AliasFor target and an alias cycle are explicit invalid identities;
    they must never look like a valid storage location to a proof engine.
    """
    value = ref.strip()
    tag, scope, suffix = _find_tag(project, value, default_program)
    if tag is None:
        if _resolving_alias:
            return "unresolved-alias", f"{scope.casefold()}:{value.casefold()}"
        return scope, value.casefold()

    key = (scope.casefold(), tag.name.casefold())
    if key in _seen:
        return "alias-cycle", f"{scope.casefold()}:{tag.name.casefold()}{suffix.casefold()}"

    alias_for = (tag.alias_for or "").strip()
    if alias_for:
        target = alias_for + suffix
        return canonical_tag_identity(
            project,
            target,
            _program_from_scope(scope),
            _seen=_seen | {key},
            _resolving_alias=True,
        )

    return scope.casefold(), tag.name.casefold() + suffix.casefold()


def identity_is_resolved(identity: tuple[str, str]) -> bool:
    return identity[0] not in _INVALID_IDENTITY_SCOPES


def storage_identities_overlap(left: tuple[str, str], right: tuple[str, str]) -> bool:
    """Return whether two canonical refs can address overlapping Rockwell storage.

    Whole-tag writes overlap member/array-element aliases (Status vs Status.0,
    Array vs Array[2]). Sibling members do not overlap. Invalid alias identities
    never participate in a proof-positive overlap relation.
    """
    if not identity_is_resolved(left) or not identity_is_resolved(right):
        return False
    if left[0] != right[0]:
        return False
    a, b = left[1], right[1]
    if a == b:
        return True
    return (
        b.startswith(a + ".")
        or b.startswith(a + "[")
        or a.startswith(b + ".")
        or a.startswith(b + "[")
    )


def canonical_writer_sources(project, ref: str, default_program: str | None) -> tuple[str, ...]:
    """Return unique external executable sources touching overlapping storage.

    RLL statement mirrors of a parsed rung are deduplicated by source location.
    AOI-internal parameter writes are not global writers; proven AOI call
    bindings already surface external writes on the caller rung. Unsupported or
    partial program ST writes still count so they can block false proof.
    """
    target = canonical_tag_identity(project, ref, default_program)
    if not identity_is_resolved(target):
        return ()
    sources: dict[tuple[str, str, str, str], str] = {}

    for rung in project.rungs:
        if not any(
            storage_identities_overlap(canonical_tag_identity(project, write, rung.program), target)
            for write in rung.writes
        ):
            continue
        key = (
            str(rung.source.aoi or ""),
            str(rung.source.program or rung.program or ""),
            str(rung.source.routine or rung.routine or ""),
            str(rung.source.rung if rung.source.rung is not None else rung.number),
        )
        sources.setdefault(key, rung.id)

    for statement in project.logic_statements:
        if statement.owner_type == "aoi":
            continue
        statement_program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        if not any(
            storage_identities_overlap(
                canonical_tag_identity(project, write, statement_program),
                target,
            )
            for write in statement.writes
        ):
            continue
        key = (
            str(statement.source.aoi or ""),
            str(statement.source.program or statement_program or ""),
            str(statement.source.routine or statement.routine or ""),
            str(
                statement.source.rung
                if statement.source.rung is not None
                else statement.source.line
                if statement.source.line is not None
                else statement.locator
            ),
        )
        sources.setdefault(key, statement.id)

    return tuple(sorted(sources.values(), key=str.casefold))


def distinct_named_tag_identities(project, name: str) -> tuple[tuple[str, str], ...]:
    """Return physical identities for every exported tag sharing one spelling."""
    identities = set()
    for tag in project.tags:
        if tag.name.casefold() != name.casefold():
            continue
        identities.add(
            canonical_tag_identity(project, tag.name, _program_from_scope(tag.scope))
        )
    return tuple(sorted(identities))


def _has_other_writer(project, model) -> bool:
    current_key = (
        str(model.source.aoi or ""),
        str(model.source.program or model.program or ""),
        str(model.source.routine or ""),
        str(model.source.rung or ""),
    )
    target = canonical_tag_identity(project, model.output_tag, model.program)
    if not identity_is_resolved(target):
        return True

    for rung in project.rungs:
        key = (
            str(rung.source.aoi or ""),
            str(rung.source.program or rung.program or ""),
            str(rung.source.routine or rung.routine or ""),
            str(rung.source.rung if rung.source.rung is not None else rung.number),
        )
        if key == current_key:
            continue
        if any(
            storage_identities_overlap(canonical_tag_identity(project, write, rung.program), target)
            for write in rung.writes
        ):
            return True

    for statement in project.logic_statements:
        if statement.owner_type == "aoi":
            continue
        statement_program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        key = (
            str(statement.source.aoi or ""),
            str(statement.source.program or statement_program or ""),
            str(statement.source.routine or statement.routine or ""),
            str(
                statement.source.rung
                if statement.source.rung is not None
                else statement.source.line
                if statement.source.line is not None
                else statement.locator
            ),
        )
        if key == current_key:
            continue
        if any(
            storage_identities_overlap(
                canonical_tag_identity(project, write, statement_program),
                target,
            )
            for write in statement.writes
        ):
            return True
    return False


def compare_models(project):
    """Apply alias/storage-aware writer identity after all existing V8 guards."""
    result = []
    for model in _PREVIOUS_COMPARE_MODELS(project):
        identity = canonical_tag_identity(project, model.output_tag, model.program)
        if not identity_is_resolved(identity):
            continue
        result.append(replace(model, single_writer=not _has_other_writer(project, model)))
    return result


def install() -> None:
    # rockwell_compare_hardening's FAT/check/requirement functions resolve their
    # module-global compare_models at call time, so patch both modules before
    # production_verification imports the theorem.
    _hard.compare_models = compare_models
    _base.compare_models = compare_models


__all__ = [
    "canonical_tag_identity",
    "canonical_writer_sources",
    "distinct_named_tag_identities",
    "identity_is_resolved",
    "storage_identities_overlap",
    "compare_models",
    "install",
]
