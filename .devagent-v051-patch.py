from __future__ import annotations

import json
from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Provider/model qualification labels.
replace(
    "devagent/config.py",
    '''_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {\n    "openai": ("gpt-5", "OPENAI_API_KEY"),\n''',
    '''_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {\n    "openai": ("gpt-5", "OPENAI_API_KEY"),\n''',
)
replace(
    "devagent/config.py",
    '''}\n\n\n@dataclass(frozen=True)\nclass ProviderConfig:\n''',
    '''}\n\n_PROVIDER_QUALIFICATION: dict[str, str] = {\n    "openai": "CONTRACT-QUALIFIED",\n    "anthropic": "CONTRACT-QUALIFIED",\n    "claude": "CONTRACT-QUALIFIED",\n    "xai": "CONTRACT-QUALIFIED",\n    "grok": "CONTRACT-QUALIFIED",\n    "gemini": "CONTRACT-QUALIFIED",\n    "google": "CONTRACT-QUALIFIED",\n    "compatible": "SUPPORTED",\n    "fake": "TEST-ONLY",\n}\n\n\n@dataclass(frozen=True)\nclass ProviderConfig:\n''',
)
replace(
    "devagent/config.py",
    '''def provider_defaults(provider: str) -> tuple[str, str]:\n    return _PROVIDER_DEFAULTS.get(provider.lower(), _PROVIDER_DEFAULTS["compatible"])\n\n\n''',
    '''def provider_defaults(provider: str) -> tuple[str, str]:\n    return _PROVIDER_DEFAULTS.get(provider.lower(), _PROVIDER_DEFAULTS["compatible"])\n\n\ndef provider_qualification(provider: str) -> str:\n    """Return the bounded qualification level for a provider adapter.\n\n    CONTRACT-QUALIFIED means deterministic provider-contract tests exist. It is not a\n    claim that a specific paid model/API key is currently reachable; `doctor --live`\n    performs that explicit runtime check.\n    """\n\n    return _PROVIDER_QUALIFICATION.get(provider.lower(), "EXPERIMENTAL")\n\n\n''',
)

# Routing output exposes bounded qualification without credentials.
replace(
    "devagent/routing.py",
    '''from devagent.config import ProviderConfig, ROLE_NAMES\n''',
    '''from devagent.config import ProviderConfig, ROLE_NAMES, provider_qualification\n''',
)
replace(
    "devagent/routing.py",
    '''    lines = [f"default: {default_config.provider}/{default_config.model}"]\n    for role in ROLE_NAMES:\n        config = role_configs.get(role, default_config)\n        suffix = "" if role in role_configs else " (default)"\n        lines.append(f"{role}: {config.provider}/{config.model}{suffix}")\n''',
    '''    lines = [\n        f"default: {default_config.provider}/{default_config.model} "\n        f"[{provider_qualification(default_config.provider)}]"\n    ]\n    for role in ROLE_NAMES:\n        config = role_configs.get(role, default_config)\n        suffix = "" if role in role_configs else " (default)"\n        lines.append(\n            f"{role}: {config.provider}/{config.model} "\n            f"[{provider_qualification(config.provider)}]{suffix}"\n        )\n''',
)

# Live readiness probe + provider-switch base URL isolation.
replace(
    "devagent/cli.py",
    '''_PROVIDER_CHOICES = ("openai", "anthropic", "claude", "xai", "grok", "gemini", "google", "compatible")\n\n\n''',
    '''_PROVIDER_CHOICES = ("openai", "anthropic", "claude", "xai", "grok", "gemini", "google", "compatible")\n_LIVE_PROBE_SCHEMA = {\n    "type": "object",\n    "properties": {"ok": {"type": "boolean", "const": True}},\n    "required": ["ok"],\n    "additionalProperties": False,\n}\n\n\n''',
)
replace(
    "devagent/cli.py",
    '''def _doctor() -> int:\n    config = load_config()\n    role_configs = load_role_configs()\n    checks = {\n        "git": shutil.which("git") is not None,\n        "configuration": config_path().is_file(),\n        "provider_sdk": _sdk_available(config),\n        "api_key": _api_key_available(config),\n    }\n    print("DEVAGENT DOCTOR")\n    for name, okay in checks.items():\n        print(f"{'OK' if okay else 'WARN'}  {name}")\n    for role in ROLE_NAMES:\n        role_config = role_configs.get(role)\n        if role_config is None:\n            continue\n        okay = _sdk_available(role_config) and _api_key_available(role_config)\n        print(\n            f"{'OK' if okay else 'WARN'}  role:{role} "\n            f"{role_config.provider}/{role_config.model}"\n        )\n    if not checks["configuration"]:\n        print("Run `devagent setup` before a cloud-provider engineering run.")\n    return 0\n''',
    '''def _live_probe(label: str, config: ProviderConfig) -> bool:\n    try:\n        response = create_provider(config).request(\n            role="doctor",\n            payload={\n                "instruction": "Return ok=true. This is a DevAgent structured-output readiness probe."\n            },\n            schema=_LIVE_PROBE_SCHEMA,\n        )\n    except ProviderError as exc:\n        print(f"LIVE FAIL  {label} {config.provider}/{config.model}: {exc}")\n        return False\n    okay = response.get("ok") is True\n    print(\n        f"{'LIVE PASS' if okay else 'LIVE FAIL'}  {label} "\n        f"{config.provider}/{config.model}"\n    )\n    return okay\n\n\ndef _doctor(*, live: bool = False) -> int:\n    config = load_config()\n    role_configs = load_role_configs()\n    checks = {\n        "git": shutil.which("git") is not None,\n        "configuration": config_path().is_file(),\n        "provider_sdk": _sdk_available(config),\n        "api_key": _api_key_available(config),\n    }\n    print("DEVAGENT DOCTOR")\n    all_ok = True\n    for name, okay in checks.items():\n        print(f"{'OK' if okay else 'WARN'}  {name}")\n        all_ok = all_ok and okay\n    role_readiness: dict[str, bool] = {}\n    for role in ROLE_NAMES:\n        role_config = role_configs.get(role)\n        if role_config is None:\n            continue\n        okay = _sdk_available(role_config) and _api_key_available(role_config)\n        role_readiness[role] = okay\n        all_ok = all_ok and okay\n        print(\n            f"{'OK' if okay else 'WARN'}  role:{role} "\n            f"{role_config.provider}/{role_config.model}"\n        )\n    if not checks["configuration"]:\n        print("Run `devagent setup` before a cloud-provider engineering run.")\n    if live:\n        if checks["provider_sdk"] and checks["api_key"]:\n            all_ok = _live_probe("default", config) and all_ok\n        else:\n            print("LIVE SKIP  default static readiness failed")\n            all_ok = False\n        for role in ROLE_NAMES:\n            role_config = role_configs.get(role)\n            if role_config is None:\n                continue\n            if role_readiness.get(role, False):\n                all_ok = _live_probe(f"role:{role}", role_config) and all_ok\n            else:\n                print(f"LIVE SKIP  role:{role} static readiness failed")\n                all_ok = False\n    return 0 if all_ok else 1\n''',
)
replace(
    "devagent/cli.py",
    '''        config = ProviderConfig(\n            provider=selected_provider,\n            model=args.model or default_model,\n            base_url=args.base_url or configured.base_url,\n            api_key_env=default_key_env,\n            timeout_seconds=configured.timeout_seconds,\n        )\n''',
    '''        inherited_base_url = (\n            configured.base_url\n            if not args.provider or args.provider == configured.provider\n            else None\n        )\n        config = ProviderConfig(\n            provider=selected_provider,\n            model=args.model or default_model,\n            base_url=args.base_url if args.base_url is not None else inherited_base_url,\n            api_key_env=default_key_env,\n            timeout_seconds=configured.timeout_seconds,\n        )\n''',
)
replace(
    "devagent/cli.py",
    '''    if arguments and arguments[0] == "doctor":\n        argparse.ArgumentParser(prog="devagent doctor", description="Check local DevAgent readiness").parse_args(arguments[1:])\n        return _doctor()\n''',
    '''    if arguments and arguments[0] == "doctor":\n        parser = argparse.ArgumentParser(\n            prog="devagent doctor",\n            description="Check local DevAgent readiness; --live performs real structured provider probes",\n        )\n        parser.add_argument(\n            "--live",\n            action="store_true",\n            help="Send one minimal structured-output probe to the default and configured role models",\n        )\n        return _doctor(live=parser.parse_args(arguments[1:]).live)\n''',
)

# Deterministic publication must not execute repository-controlled hooks.
replace(
    "devagent/source_control.py",
    '''import subprocess\nfrom dataclasses import dataclass\n''',
    '''import subprocess\nimport tempfile\nfrom dataclasses import dataclass\n''',
)
replace(
    "devagent/source_control.py",
    '''def _failure(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:\n''',
    '''def _git_without_hooks(\n    root: Path,\n    *args: str,\n    timeout: int = 60,\n) -> subprocess.CompletedProcess[str]:\n    """Run deterministic publication Git with repository-controlled hooks disabled."""\n\n    with tempfile.TemporaryDirectory(prefix="devagent-empty-git-hooks-") as hooks_dir:\n        return _git(\n            root,\n            "-c",\n            f"core.hooksPath={hooks_dir}",\n            *args,\n            timeout=timeout,\n        )\n\n\ndef _failure(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:\n''',
)
replace(
    "devagent/source_control.py",
    '''    commit = _git(working_root, "commit", "-m", message, timeout=30)\n''',
    '''    commit = _git_without_hooks(working_root, "commit", "-m", message, timeout=30)\n''',
)
replace(
    "devagent/source_control.py",
    '''        push = _git(working_root, "push", remote, f"HEAD:refs/heads/{target_branch}")\n    else:\n        push = _git(working_root, "push", "--set-upstream", remote, target_branch)\n''',
    '''        push = _git_without_hooks(\n            working_root, "push", remote, f"HEAD:refs/heads/{target_branch}"\n        )\n    else:\n        push = _git_without_hooks(\n            working_root, "push", "--set-upstream", remote, target_branch\n        )\n''',
)

# Tests: live doctor, provider switch isolation, visible qualification labels.
cli = Path("tests/test_cli.py")
text = cli.read_text(encoding="utf-8")
text = text.replace(
    '''from devagent.source_control import PublicationPlan\n''',
    '''from devagent.providers import ProviderError\nfrom devagent.source_control import PublicationPlan\n''',
    1,
)
text += '''\n\ndef test_doctor_live_probes_default_and_role_models(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n    capsys: pytest.CaptureFixture[str],\n) -> None:\n    config_path = tmp_path / "config.toml"\n    config_path.write_text("[provider]\\nname='openai'\\nmodel='gpt-live'\\n", encoding="utf-8")\n    monkeypatch.setenv("DEVAGENT_CONFIG", str(config_path))\n    default = ProviderConfig("openai", "gpt-live", api_key_env="OPENAI_API_KEY")\n    reviewer = ProviderConfig("anthropic", "claude-live", api_key_env="ANTHROPIC_API_KEY")\n    monkeypatch.setattr("devagent.cli.load_config", lambda: default)\n    monkeypatch.setattr("devagent.cli.load_role_configs", lambda: {"reviewer": reviewer})\n    monkeypatch.setattr("devagent.cli._sdk_available", lambda _config: True)\n    monkeypatch.setattr("devagent.cli._api_key_available", lambda _config: True)\n    calls: list[str] = []\n\n    class LiveProvider:\n        def __init__(self, label: str) -> None:\n            self.label = label\n\n        def request(self, *, role, payload, schema):\n            calls.append(self.label)\n            assert role == "doctor"\n            assert schema["properties"]["ok"]["const"] is True\n            return {"ok": True}\n\n    monkeypatch.setattr(\n        "devagent.cli.create_provider",\n        lambda config: LiveProvider(f"{config.provider}/{config.model}"),\n    )\n\n    assert main(["doctor", "--live"]) == 0\n    output = capsys.readouterr().out\n    assert "LIVE PASS  default openai/gpt-live" in output\n    assert "LIVE PASS  role:reviewer anthropic/claude-live" in output\n    assert calls == ["openai/gpt-live", "anthropic/claude-live"]\n\n\ndef test_doctor_live_failure_returns_nonzero(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n    capsys: pytest.CaptureFixture[str],\n) -> None:\n    config_path = tmp_path / "config.toml"\n    config_path.write_text("[provider]\\nname='openai'\\nmodel='broken-model'\\n", encoding="utf-8")\n    monkeypatch.setenv("DEVAGENT_CONFIG", str(config_path))\n    monkeypatch.setattr(\n        "devagent.cli.load_config",\n        lambda: ProviderConfig("openai", "broken-model", api_key_env="OPENAI_API_KEY"),\n    )\n    monkeypatch.setattr("devagent.cli.load_role_configs", lambda: {})\n    monkeypatch.setattr("devagent.cli._sdk_available", lambda _config: True)\n    monkeypatch.setattr("devagent.cli._api_key_available", lambda _config: True)\n\n    class BrokenProvider:\n        def request(self, **_kwargs):\n            raise ProviderError("probe failed")\n\n    monkeypatch.setattr("devagent.cli.create_provider", lambda _config: BrokenProvider())\n    assert main(["doctor", "--live"]) == 1\n    assert "LIVE FAIL  default openai/broken-model" in capsys.readouterr().out\n\n\ndef test_explicit_provider_switch_does_not_inherit_previous_base_url(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    events: list[str] = []\n    repo, _result = _patch_engineering_run(tmp_path, monkeypatch, events)\n    monkeypatch.setattr(\n        "devagent.cli.load_config",\n        lambda: ProviderConfig(\n            "compatible",\n            "local-model",\n            "http://127.0.0.1:11434/v1",\n            "DEVAGENT_API_KEY",\n        ),\n    )\n    monkeypatch.setattr("devagent.cli.load_role_configs", lambda: {})\n    captured: list[ProviderConfig] = []\n\n    def capture(config: ProviderConfig):\n        captured.append(config)\n        return object()\n\n    monkeypatch.setattr("devagent.cli.create_provider", capture)\n    assert main(\n        [\n            "--repo",\n            str(repo),\n            "--no-publish",\n            "--provider",\n            "openai",\n            "--model",\n            "gpt-test",\n            "Add multiplication support",\n        ]\n    ) == 0\n    assert captured[0].provider == "openai"\n    assert captured[0].base_url is None\n\n\ndef test_models_show_bounded_provider_qualification(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n    capsys: pytest.CaptureFixture[str],\n) -> None:\n    path = tmp_path / "config.toml"\n    monkeypatch.setenv("DEVAGENT_CONFIG", str(path))\n    assert main(["setup", "--provider", "openai", "--model", "gpt-default"]) == 0\n    assert main(["models"]) == 0\n    output = capsys.readouterr().out\n    assert "default: openai/gpt-default [CONTRACT-QUALIFIED]" in output\n\n'''
cli.write_text(text, encoding="utf-8")

# Tests: repository hooks cannot execute during deterministic commit/push.
source_test = Path("tests/test_source_control_publish.py")
text = source_test.read_text(encoding="utf-8")
text += '''\n\ndef test_publication_disables_repository_git_hooks(tmp_path: Path) -> None:\n    source, working, _remote = _repo_with_bare_remote(tmp_path)\n    hooks = source / ".git" / "hooks"\n    for name in ("pre-commit", "pre-push"):\n        hook = hooks / name\n        hook.write_text("#!/bin/sh\\nexit 97\\n", encoding="utf-8")\n        hook.chmod(0o755)\n\n    (working / "calculator.py").write_text(\n        "def divide(a, b):\\n    return a / b\\n\\ndef multiply(a, b):\\n    return a * b\\n",\n        encoding="utf-8",\n    )\n    publication = publish_verified_branch(\n        _result(source, working),\n        branch="devagent/hooks-disabled",\n    )\n\n    assert publication.committed is True\n    assert publication.pushed is True\n    assert publication.error is None\n\n'''
source_test.write_text(text, encoding="utf-8")

# Qualification catalog locks the trust/security fixes.
catalog = Path("evaluation/benchmark_v4.json")
payload = json.loads(catalog.read_text(encoding="utf-8"))
existing = {item["id"] for item in payload["cases"]}
new_cases = [
    {
        "id": "provider-switch-base-url-isolated",
        "category": "provider_contract",
        "pytest_node": "tests/test_cli.py::test_explicit_provider_switch_does_not_inherit_previous_base_url",
        "expected": "PASS",
    },
    {
        "id": "doctor-live-structured-probe",
        "category": "provider_contract",
        "pytest_node": "tests/test_cli.py::test_doctor_live_probes_default_and_role_models",
        "expected": "PASS",
    },
    {
        "id": "provider-qualification-visible",
        "category": "model_routing",
        "pytest_node": "tests/test_cli.py::test_models_show_bounded_provider_qualification",
        "expected": "PASS",
    },
    {
        "id": "publication-disables-repository-hooks",
        "category": "source_control_safety",
        "pytest_node": "tests/test_source_control_publish.py::test_publication_disables_repository_git_hooks",
        "expected": "PASS",
    },
]
for item in new_cases:
    if item["id"] not in existing:
        payload["cases"].append(item)
catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

# v0.5.1 version + docs.
replace("pyproject.toml", 'version = "0.5.0"', 'version = "0.5.1"')
replace("devagent/__init__.py", '__version__ = "0.5.0"', '__version__ = "0.5.1"')

readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
text = text.replace(
    "devagent doctor\n",
    "devagent doctor\ndevagent doctor --live\n",
    1,
)
text = text.replace(
    "Provider choice affects reasoning quality, cost, latency, and privacy characteristics. It does not change DevAgent's deterministic acceptance, safety, verification, reporting, and publication rules.",
    "Provider choice affects reasoning quality, cost, latency, and privacy characteristics. It does not change DevAgent's deterministic acceptance, safety, verification, reporting, and publication rules. `devagent models` labels deterministic adapter status as `CONTRACT-QUALIFIED` or `SUPPORTED`; `devagent doctor --live` is the explicit real API/model structured-output readiness probe and consumes provider usage.",
)
text = text.replace(
    "- publication is a separate deterministic post-report step;\n",
    "- publication is a separate deterministic post-report step and disables repository-controlled Git hooks for commit/push;\n",
)
readme.write_text(text, encoding="utf-8")

changelog = Path("CHANGELOG.md")
text = changelog.read_text(encoding="utf-8")
entry = '''## 0.5.1 - Trust and security hardening\n\n- Prevent explicit provider switches from inheriting a stale base URL from a different configured provider.\n- Add `devagent doctor --live` structured provider/model probes with non-zero failure status.\n- Expose bounded provider adapter status in `devagent models` without overstating paid-model qualification.\n- Disable repository-controlled Git hooks during deterministic VERIFIED commit/push publication.\n- Extend Production Qualification v4 with trust/security regression cases.\n\n'''
if "## 0.5.1 - Trust and security hardening" not in text:
    marker = "# Changelog\n\n"
    if marker not in text:
        raise SystemExit("CHANGELOG marker not found")
    text = text.replace(marker, marker + entry, 1)
    changelog.write_text(text, encoding="utf-8")

print("v0.5.1 patch applied")
