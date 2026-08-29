from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveOpcUaRuntimeStatus:
    installed: bool
    version: str | None
    supported: bool
    detail: str


def detect_live_opcua_runtime() -> LiveOpcUaRuntimeStatus:
    if importlib.util.find_spec("asyncua") is None:
        return LiveOpcUaRuntimeStatus(
            installed=False,
            version=None,
            supported=False,
            detail=(
                'asyncua is not installed; install the Live runtime with '
                'python -m pip install "devagent-ai[live]".'
            ),
        )
    try:
        version = importlib.metadata.version("asyncua")
    except importlib.metadata.PackageNotFoundError:
        return LiveOpcUaRuntimeStatus(
            installed=True,
            version=None,
            supported=False,
            detail="asyncua is importable but package version metadata is unavailable.",
        )
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        return LiveOpcUaRuntimeStatus(
            installed=True,
            version=version,
            supported=False,
            detail=f"asyncua version {version!r} cannot be qualified against the supported >=2,<3 range.",
        )
    if major != 2:
        return LiveOpcUaRuntimeStatus(
            installed=True,
            version=version,
            supported=False,
            detail=f"asyncua {version} is outside the supported >=2,<3 production range.",
        )
    return LiveOpcUaRuntimeStatus(
        installed=True,
        version=version,
        supported=True,
        detail=f"asyncua {version} is inside the supported >=2,<3 production range.",
    )


__all__ = ["LiveOpcUaRuntimeStatus", "detect_live_opcua_runtime"]
