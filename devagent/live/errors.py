from __future__ import annotations


class LiveError(RuntimeError):
    """Base error for DevAgent Live."""


class LiveDependencyError(LiveError):
    """Raised when the optional OPC UA runtime dependency is not installed."""


class LiveConnectionError(LiveError):
    """Raised when an OPC UA endpoint cannot be reached or a session cannot be used."""


class LiveConfigurationError(LiveError):
    """Raised for invalid or unsafe DevAgent Live configuration."""
