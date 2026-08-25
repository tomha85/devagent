from __future__ import annotations

from devagent.config import ProviderConfig
from devagent.provider_benchmark import BenchmarkTarget, qualification_targets, run_benchmark
from devagent.providers import ProviderError, ScriptedFakeProvider


def test_provider_benchmark_deduplicates_same_provider_model_endpoint() -> None:
    default = ProviderConfig("openai", "gpt-test", None, "OPENAI_API_KEY", 30)
    roles = {
        "planner": ProviderConfig("openai", "gpt-test", None, "OTHER_KEY", 30),
        "reviewer": ProviderConfig("anthropic", "claude-test", None, "ANTHROPIC_API_KEY", 30),
    }

    targets = qualification_targets(default, roles)

    assert [(item.label, item.config.provider, item.config.model) for item in targets] == [
        ("default", "openai", "gpt-test"),
        ("role:reviewer", "anthropic", "claude-test"),
    ]


def test_provider_benchmark_executes_strict_live_contract_with_factory() -> None:
    target = BenchmarkTarget("default", ProviderConfig("openai", "gpt-test", None, "OPENAI_API_KEY", 30))

    results = run_benchmark(
        (target,),
        provider_factory=lambda config: ScriptedFakeProvider(
            [{"ok": True, "contract": "devagent-provider-benchmark-v1"}]
        ),
    )

    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].error is None
    assert results[0].latency_seconds >= 0


def test_provider_benchmark_redacts_secret_values_from_errors(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    target = BenchmarkTarget("default", ProviderConfig("openai", "gpt-test", None, "OPENAI_API_KEY", 30))

    class _Broken:
        def request(self, **kwargs):
            raise ProviderError("failure contains super-secret-value")

    results = run_benchmark((target,), provider_factory=lambda config: _Broken())

    assert results[0].passed is False
    assert "super-secret-value" not in (results[0].error or "")
    assert "[REDACTED]" in (results[0].error or "")
