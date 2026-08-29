from __future__ import annotations

from .engineering_context import (
    LiveEngineeringContext,
    LiveEngineeringTag,
    normalize_engineering_identifier,
)


def resolve_explicit_tag_reference(
    context: LiveEngineeringContext,
    question: str,
) -> LiveEngineeringTag | None:
    """Resolve an explicitly named canonical engineering tag from a question.

    This resolver is intentionally stricter than conversational follow-up reuse:
    an explicit tag identity in the current question must win over remembered
    context from an earlier question. Ambiguous same-length matches fail closed.
    """

    normalized_question = normalize_engineering_identifier(question)
    if not normalized_question:
        return None

    matches: list[tuple[int, LiveEngineeringTag]] = []
    for tag in context.tags:
        identities = tuple(
            identity
            for identity in tag.identity_forms()
            if len(identity) >= 3 and identity in normalized_question
        )
        if identities:
            matches.append((max(len(identity) for identity in identities), tag))

    if not matches:
        return None

    longest = max(length for length, _tag in matches)
    narrowed = [tag for length, tag in matches if length == longest]
    unique_by_id = {tag.id: tag for tag in narrowed}
    if len(unique_by_id) != 1:
        return None
    return next(iter(unique_by_id.values()))


__all__ = ["resolve_explicit_tag_reference"]
