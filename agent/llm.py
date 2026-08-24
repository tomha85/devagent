"""Compatibility exports for provider abstractions."""

from devagent.providers import ModelProvider as LLMClient
from devagent.providers import ProviderError as LLMError

__all__ = ["LLMClient", "LLMError"]
