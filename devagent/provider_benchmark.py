from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from devagent.config import ProviderConfig, load_config, load_role_configs
from devagent.providers import ModelProvider, ProviderError, create_provider


_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "const": True},
        "contract": {"type": "string", "const": "devagent-provider-benchmark-v1"},
    },
    "required": ["ok", "contract"],
    "additionalProperties": False,
}
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD")


@dataclass(frozen=True)
class BenchmarkTarget:
    label: str
    config: ProviderConfig


@dataclass(frozen=True)
class BenchmarkResult:
    label: str
    provider: str
    model: str
    passed: bool
    latency_seconds: float
    error: str | None = None


def qualification_targets(default: ProviderConfig, roles: dict[str, ProviderConfig]) -> tuple[BenchmarkTarget, ...]:
    candidates = [BenchmarkTarget("default", default)]
    candidates.extend(BenchmarkTarget(f"role:{name}", config) for name, config in sorted(roles.items()))
    result: list[BenchmarkTarget] = []
    seen: set[tuple[str, str, str | None]] = set()
    for target in candidates:
        key = (target.config.provider, target.config.model, target.config.base_url)
        if key not in seen:
            seen.add(key)
            result.append(target)
    return tuple(result)


def _secret_values(config: ProviderConfig) -> tuple[str, ...]:
    values: set[str] = set()
    if config.api_key_env:
        explicit = os.getenv(config.api_key_env)
        if explicit:
            values.add(explicit)
    for name, value in os.environ.items():
        normalized = name.upper()
        if value and any(marker in normalized for marker in _SECRET_ENV_MARKERS):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _sanitized_error(exc: Exception, config: ProviderConfig) -> str:
    text = str(exc)
    for value in _secret_values(config):
        text = text.replace(value, "[REDACTED]")
    return text[:500]


def run_benchmark(
    targets: tuple[BenchmarkTarget, ...],
    *,
    provider_factory: Callable[[ProviderConfig], ModelProvider] = create_provider,
) -> tuple[BenchmarkResult, ...]:
    results: list[BenchmarkResult] = []
    for target in targets:
        started = time.monotonic()
        error: str | None = None
        passed = False
        try:
            response = provider_factory(target.config).request(
                role="provider_benchmark",
                payload={
                    "instruction": "Return the exact structured benchmark contract. Do not add fields.",
                    "provider": target.config.provider,
                    "model": target.config.model,
                },
                schema=_SCHEMA,
            )
            passed = response == {"ok": True, "contract": "devagent-provider-benchmark-v1"}
            if not passed:
                error = "provider returned a schema-valid but incorrect benchmark contract"
        except (ProviderError, OSError, ValueError) as exc:
            error = _sanitized_error(exc, target.config)
        results.append(
            BenchmarkResult(
                label=target.label,
                provider=target.config.provider,
                model=target.config.model,
                passed=passed,
                latency_seconds=time.monotonic() - started,
                error=error,
            )
        )
    return tuple(results)


def _write_report(path: Path, results: tuple[BenchmarkResult, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_epoch": int(time.time()),
        "passed": all(item.passed for item in results),
        "results": [asdict(item) for item in results],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continuously qualify configured real model providers")
    parser.add_argument("--report", type=Path, default=Path(".devagent/provider-benchmark.json"))
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="Repeat in the foreground at this interval; 0 runs once, minimum recurring interval is 300 seconds",
    )
    args = parser.parse_args(argv)
    if args.interval_seconds and args.interval_seconds < 300:
        parser.error("--interval-seconds must be 0 or at least 300")

    while True:
        targets = qualification_targets(load_config(), load_role_configs())
        results = run_benchmark(targets)
        _write_report(args.report, results)
        print(json.dumps([asdict(item) for item in results], indent=2))
        if not args.interval_seconds:
            return 0 if results and all(item.passed for item in results) else 1
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
