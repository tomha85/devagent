"""Compatibility exports for workspace safety types."""

from devagent.safety import SafetyError as ToolError
from devagent.workspace import Workspace as WorkspaceTools

__all__ = ["ToolError", "WorkspaceTools"]
