# DevAgent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)

**A local, evidence-driven software engineering agent that turns a requirement into a tested, reviewed patch — while leaving commit, push, merge, and deploy decisions to the developer.**

DevAgent runs against a local repository. It discovers the application, gathers source evidence, compiles acceptance criteria, plans a bounded change, creates backups, implements the minimum necessary patch, runs repository-supported verification, reviews the final diff, and returns one final engineering report.

> **From requirement to verified local branch.**
>
> DevAgent can modify software. It does not publish software.

## Why DevAgent?

Many coding agents optimize for generating code quickly. DevAgent is designed around a different question:

**Can the change be supported by repository evidence and verified locally?**

Core principles:

- **Local-first** — works against the developer's local repository and environment.
- **Evidence before modification** — implementation is blocked when source evidence is insufficient.
- **Minimal-change discipline** — prefer the smallest correct diff over broad refactors.
- **Backups before edits** — existing files are backed up before first modification.
- **Repository-native verification** — use commands supported by manifests, package scripts, tests, and CI evidence.
- **Independent review** — the final diff is reviewed separately from implementation.
- **Provider choice** — OpenAI, Anthropic/Claude, xAI/Grok, and OpenAI-compatible local endpoints.
- **No automatic publishing** — DevAgent does not commit, push, merge, rebase, or deploy.
- **Evidence-backed outcomes** — final status is `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED`.

## Quick start

### Install

Using `pipx`:

```bash
pipx install git+https://github.com/tomha85/devagent.git
```

Or from a checkout:

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

### Configure an AI provider

OpenAI example:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor
```

Anthropic example:

```bash
devagent setup --provider anthropic --model YOUR_MODEL
export ANTHROPIC_API_KEY=...
devagent doctor
```

OpenAI-compatible local endpoint:

```bash
devagent setup \
  --provider compatible \
  --model local-model \
  --base-url http://127.0.0.1:11434/v1
```

DevAgent stores provider configuration and the **name** of the API-key environment variable. It does not store the API key itself.

### Run

From the application repository you want DevAgent to work on:

```bash
devagent "Fix websocket reconnect bug and add regression tests"
```

Other inputs:

```bash
devagent "Add CSV export to reports"
devagent --input error.log
pytest 2>&1 | devagent
devagent
```

Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent status
```

Normal mode stays quiet apart from state updates and the final engineering report. `--verbose` exposes operational diagnostics, not hidden chain-of-thought.

## What DevAgent does

A normal engineering run follows a deterministic lifecycle:

```text
PREFLIGHT
  ↓
DISCOVER
  ↓
UNDERSTAND
  ↓
TASK_SPEC / ACCEPTANCE CRITERIA
  ↓
BASELINE
  ↓
PLAN
  ↓
GATHER_CONTEXT
  ↓
REPRODUCE
  ↓
IMPLEMENT MINIMAL PATCH
  ↓
VERIFY_TARGETED
  ↓
DIAGNOSE / REPLAN when needed
  ↓
VERIFY_BROAD
  ↓
INDEPENDENT REVIEW
  ↓
QUALITY_CHECK
  ↓
FINAL_VERIFY
  ↓
LEARN
  ↓
REPORT
```

Python owns deterministic state transitions, safety gates, filesystem operations, verification validity, retry bounds, and final status. The model reasons inside bounded roles.

## Evidence gate

DevAgent does not treat model confidence as evidence.

Before implementation, it expects enough repository evidence to support:

- the engineering problem,
- expected behavior,
- affected paths,
- likely root cause or design location,
- a minimal proposed solution,
- source evidence tying those claims to the repository.

If the evidence is insufficient, `BLOCKED` is the correct result.

## Verification model

Verification is evidence-driven and revision-aware.

Depending on the repository, DevAgent can run:

- baseline tests,
- targeted tests,
- broader unit/component tests,
- functional or integration checks,
- build commands,
- lint/type checks,
- `git diff --check`,
- final current-revision verification.

Each verification result records the command, exit code, duration, output, failure classification, phase, and workspace revision.

A code modification invalidates prior successful verification for the old revision.

## Outcome contract

### `VERIFIED`

Used only when the current implementation has sufficient acceptance evidence, applicable verification passes, no known new regression remains, scope is acceptable, and independent review approves the final diff.

### `PARTIALLY_VERIFIED`

Used when implementation evidence exists but meaningful verification cannot be completed, commonly because of environmental, hardware, VPN, credential, or external-service limitations.

### `BLOCKED`

Used when DevAgent cannot safely understand, implement, or verify the task.

DevAgent is intentionally conservative: a truthful `BLOCKED` is better than a false `VERIFIED`.

## Safety boundary

DevAgent uses defense-in-depth controls around repository modification and command execution.

- Clean Git repositories use a retained detached worktree by default.
- Pre-existing dirty developer files are protected.
- Existing files are backed up before their first modification.
- Workspace paths are confined and checked against symlink escape.
- Secret-like paths such as `.env*`, private keys, SSH/AWS credentials, and generated dependency trees are excluded from automatic reads.
- Commands run as argv without a shell.
- Credential environment variables are scrubbed from verification subprocesses where appropriate.
- Publishing and destructive operations are blocked.
- DevAgent never automatically commits, pushes, merges, rebases, or deploys.

DevAgent is **not** an operating-system sandbox. Always review the final report and diff before publishing changes.

## Repository intelligence

DevAgent discovers repository structure from source files and repository-native evidence such as:

- `README` / contribution documentation,
- `pyproject.toml`, `requirements.txt`, `pytest.ini`,
- `package.json`, lockfiles, TypeScript configuration,
- `Cargo.toml`, `go.mod`,
- Maven and Gradle files,
- CMake / Make / Meson,
- `.sln` / `.csproj`,
- Docker and Compose files,
- GitHub Actions, Jenkins, GitLab CI, and Azure Pipelines.

Current discovery supports Python, JavaScript/TypeScript, React-style projects, Go, Rust, Java, C/C++, .NET, Make-based projects, and multi-component repositories.

CI is treated as executable documentation when it provides safe, bounded command evidence.

## Provider architecture

The engineering workflow is model-independent. Providers implement a common request contract.

Currently supported:

| Provider | Configuration |
| --- | --- |
| OpenAI | `--provider openai` |
| Anthropic / Claude | `--provider anthropic` |
| xAI / Grok | `--provider xai` |
| Local / compatible | `--provider compatible --base-url ...` |

The goal is to let developers choose the model that fits their accuracy, privacy, latency, and cost requirements without changing the core engineering workflow.

## Example final report

```text
DEVAGENT REPORT

STATUS
VERIFIED

TASK
Handle division by zero safely and add a regression test.

ROOT CAUSE
The divide path did not explicitly handle a zero divisor.

IMPLEMENTATION
- Added bounded zero-divisor handling
- Added regression coverage

FILES CHANGED
calculator.py
test_calculator.py

VERIFICATION
PASS  targeted tests
PASS  broader tests
PASS  git diff --check

NEW REGRESSIONS
None detected

SOURCE CONTROL
No commit
No push
No merge

DEVELOPER ACTION
Review the diff before publishing.
```

## Local run data

DevAgent keeps run artifacts under the target repository's `.devagent/` state:

```text
.devagent/
├── runs/<run-id>/
│   ├── metadata.json
│   ├── backups/
│   ├── observations.jsonl
│   ├── verification.json
│   └── report.json
├── worktrees/<run-id>/
└── memory/
    ├── repository.json
    └── strategies.json
```

Repository facts are tied to evidence fingerprints and can be invalidated when their source changes.

## Development

Run the test suite:

```bash
python -m compileall devagent
pytest -q
git diff --check
```

Install the current checkout in editable mode:

```bash
pip install -e ".[dev]"
```

Automated tests use a deterministic fake provider and do not consume cloud-model credits.

## Project status

DevAgent is currently **alpha software**. The core evidence-driven workflow is functional, but real-provider behavior and repository coverage are still being hardened through disposable end-to-end engineering fixtures.

Near-term focus:

- real-provider contract hardening,
- stronger repository context retrieval,
- safer verification-capability discovery,
- worktree lifecycle improvements,
- more realistic multi-language evaluation fixtures,
- packaging and release quality.

The project intentionally prioritizes trustworthy outcomes over feature count.

## Contributing

Contributions are welcome.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. In particular, changes to DevAgent's safety or verification behavior should include regression tests and must not weaken the no-publish boundary.

For bugs and feature requests, use the [GitHub issue tracker](https://github.com/tomha85/devagent/issues).

## Security

Please do not publish sensitive vulnerability details in a public issue.

See [SECURITY.md](SECURITY.md) for the current reporting process and security scope.

## License

DevAgent is open source under the [MIT License](LICENSE).

```text
Copyright (c) 2026 Tom Ha
```

The MIT license permits use, copying, modification, merging, publishing, distribution, sublicensing, and sale of copies, provided the copyright and permission notice are retained as required by the license.

## Author and original project

DevAgent was created by **Tom Ha**.

Original repository: **https://github.com/tomha85/devagent**

See [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT) for project attribution.
