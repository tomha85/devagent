import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

SUPPORTED_PROVIDERS = {"openai", "claude", "anthropic", "grok", "xai"}


class LLMError(RuntimeError):
    """Raised when the selected LLM provider cannot complete a request."""


class LLMClient:
    def __init__(self, provider: Optional[str] = None) -> None:
        selected = (
            provider
            or os.getenv("DEFAULT_PROVIDER")
            or os.getenv("DEVAGENT_LLM_PROVIDER")
            or "claude"
        ).strip().lower()

        aliases = {"anthropic": "claude", "xai": "grok"}
        self.provider = aliases.get(selected, selected)
        if selected not in SUPPORTED_PROVIDERS:
            raise LLMError(
                f"Unsupported provider '{selected}'. Use openai, claude, or grok."
            )

    def complete(self, *, system: str, user: str) -> str:
        if self.provider == "openai":
            return self._openai(system=system, user=user)
        if self.provider == "claude":
            return self._claude(system=system, user=user)
        if self.provider == "grok":
            return self._grok(system=system, user=user)
        raise LLMError(f"Unsupported provider: {self.provider}")

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise LLMError(f"Missing required environment variable: {name}")
        return value

    def _openai(self, *, system: str, user: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self._require_env("OPENAI_API_KEY"))
        model = os.getenv("OPENAI_MODEL", "gpt-5")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        content = response.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned an empty response")
        return content.strip()

    def _claude(self, *, system: str, user: str) -> str:
        from anthropic import Anthropic

        client = Anthropic(api_key=self._require_env("ANTHROPIC_API_KEY"))
        model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise LLMError(f"Claude request failed: {exc}") from exc

        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        if not text_parts:
            raise LLMError("Claude returned an empty response")
        return "\n".join(text_parts).strip()

    def _grok(self, *, system: str, user: str) -> str:
        from openai import OpenAI

        client = OpenAI(
            api_key=self._require_env("XAI_API_KEY"),
            base_url=os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"),
        )
        model = os.getenv("GROK_MODEL", "grok-4")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            raise LLMError(f"Grok request failed: {exc}") from exc

        content = response.choices[0].message.content
        if not content:
            raise LLMError("Grok returned an empty response")
        return content.strip()
