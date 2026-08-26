from __future__ import annotations


def require_qualified_echo_descriptor(result, descriptor) -> dict:
    registry = result.execution_backend_registry
    if not isinstance(registry, dict):
        raise ValueError("Rockwell Echo execution requires an authenticated backend registry")
    rows = registry.get("backends")
    if not isinstance(rows, list):
        raise ValueError("Rockwell Echo backend registry does not contain normalized backend entries")
    match = next((item for item in rows if item.get("id") == descriptor.adapter_id), None)
    if not isinstance(match, dict):
        raise ValueError(
            f"Rockwell Echo adapter {descriptor.adapter_id!r} is not present in the authenticated backend registry"
        )
    if str(match.get("status") or "").upper() != "QUALIFIED":
        raise ValueError(
            f"Rockwell Echo adapter {descriptor.adapter_id!r} is not QUALIFIED"
        )
    if str(match.get("kind") or "").upper() != "SIMULATOR":
        raise ValueError(
            f"Rockwell Echo adapter {descriptor.adapter_id!r} must be qualified as SIMULATOR"
        )
    scope = match.get("project_sha256")
    if isinstance(scope, str):
        values = [scope]
    elif isinstance(scope, (list, tuple)):
        values = list(scope)
    else:
        raise ValueError(
            f"Rockwell Echo adapter {descriptor.adapter_id!r} has invalid normalized project scope"
        )
    current_sha = result.engineering.project.metadata.source_sha256.lower()
    normalized = {str(item).lower() if str(item) != "*" else "*" for item in values}
    if "*" not in normalized and current_sha not in normalized:
        raise ValueError(
            f"Rockwell Echo adapter {descriptor.adapter_id!r} is not qualified for this analyzed L5X SHA-256"
        )
    return match


__all__ = ["require_qualified_echo_descriptor"]
