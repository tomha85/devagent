from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "devagent/config.py",
    '    "grok": ("grok-4", "XAI_API_KEY"),\n    "compatible": ("local-model", "DEVAGENT_API_KEY"),',
    '    "grok": ("grok-4", "XAI_API_KEY"),\n    "gemini": ("gemini-3.7-flash", "GEMINI_API_KEY"),\n    "google": ("gemini-3.7-flash", "GEMINI_API_KEY"),\n    "compatible": ("local-model", "DEVAGENT_API_KEY"),',
)

replace(
    "devagent/providers.py",
    '''    if provider in {"openai", "xai", "grok", "compatible"}:\n        if provider in {"xai", "grok"} and not config.base_url:\n            config = ProviderConfig(provider, config.model, "https://api.x.ai/v1", config.api_key_env, config.timeout_seconds)\n        return OpenAICompatibleProvider(config)\n''',
    '''    if provider in {"openai", "xai", "grok", "gemini", "google", "compatible"}:\n        if provider in {"xai", "grok"} and not config.base_url:\n            config = ProviderConfig(\n                provider,\n                config.model,\n                "https://api.x.ai/v1",\n                config.api_key_env,\n                config.timeout_seconds,\n            )\n        elif provider in {"gemini", "google"} and not config.base_url:\n            config = ProviderConfig(\n                provider,\n                config.model,\n                "https://generativelanguage.googleapis.com/v1beta/openai/",\n                config.api_key_env,\n                config.timeout_seconds,\n            )\n        return OpenAICompatibleProvider(config)\n''',
)

replace(
    "devagent/cli.py",
    '_PROVIDER_CHOICES = ("openai", "anthropic", "claude", "xai", "grok", "compatible")',
    '_PROVIDER_CHOICES = ("openai", "anthropic", "claude", "xai", "grok", "gemini", "google", "compatible")',
)

replace(
    "devagent/qualification.py",
    "import json\nimport subprocess",
    "import json\nimport os\nimport subprocess",
)

replace(
    "devagent/qualification.py",
    '''        completed = subprocess.run(\n            [sys.executable, "-m", "pytest", "-q", case.pytest_node],\n            cwd=root,\n            capture_output=True,\n            text=True,\n            check=False,\n        )\n''',
    '''        environment = os.environ.copy()\n        environment["DEVAGENT_PRODUCTION_QUALIFICATION"] = "1"\n        completed = subprocess.run(\n            [sys.executable, "-m", "pytest", "-q", case.pytest_node],\n            cwd=root,\n            env=environment,\n            capture_output=True,\n            text=True,\n            check=False,\n        )\n''',
)

replace(
    "devagent/qualification.py",
    '        default=Path("evaluation/benchmark_v2.json"),',
    '        default=Path("evaluation/benchmark_v3.json"),',
)

replace(
    "pyproject.toml",
    'version = "0.3.2"',
    'version = "0.4.0"',
)
replace(
    "pyproject.toml",
    '  "Development Status :: 3 - Alpha",',
    '  "Development Status :: 4 - Beta",',
)
replace(
    "devagent/__init__.py",
    '__version__ = "0.3.2"',
    '__version__ = "0.4.0"',
)

print("production v0.4 patch applied")
