# DevAgent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
[![Production CI](https://github.com/tomha85/devagent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomha85/devagent/actions/workflows/ci.yml)

**A local, evidence-driven software engineering agent that turns a requirement into a tested, reviewed patch — with optional verified-branch commit/push while leaving PR and merge decisions to the developer.**

DevAgent runs against a local repository. It discovers the application, gathers source evidence, compiles acceptance criteria, plans a bounded change, creates backups, implements the minimum necessary patch, runs repository-supported verification, reviews the final diff, and returns one final engineering report.

> **From requirement to evidence-backed verified branch.**
>
> DevAgent can optionally commit and push a `VERIFIED` result to a new branch. It never creates a pull request or merges code.

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
- **Bounded publishing** — opt-in publishing is allowed only after `VERIFIED`, only to a new non-protected branch, and only from the isolated worktree.
- **No PR or merge automation** — DevAgent never creates PRs, merges, rebases, force-pushes, or deploys.
- **Evidence-backed outcomes** — final status is `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED`.

## Quick start

### Install

From PyPI:

```bash
python -m pip install devagent-ai
```

For an isolated CLI installation, `pipx` is recommended:

```bash
pipx install devagent-ai
```

Or install and develop directly from a source checkout:

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

Both installation paths expose the same `devagent` CLI. The PyPI distribution is named `devagent-ai`; a source checkout remains the best option for contributors.

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

To commit and push a successful `VERIFIED` result to a new branch:

```bash
devagent --publish "Add CSV export to reports"
```

The default pushed branch is unique per run, for example `devagent/20260825T010000Z-ab12cd`. To choose the new branch name explicitly:

```bash
devagent \
  --publish-branch feature/devagent-csv-export \
  "Add CSV export to reports"
```

Publishing is intentionally narrow:

- the run must finish `VERIFIED`,
- the default isolated worktree must be active,
- the target branch must be new and cannot be `main`, `master`, or `trunk`,
- only reviewed changed paths are staged,
- one commit is created and pushed to the selected remote,
- no pull request is created,
- no merge, rebase, force push, or deployment is performed.

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

Python owns deterministic state transitions, safety gates, filesystem operations, verification validity, retry bounds, final status, and optional verified-branch publication. The model reasons inside bounded roles and never receives a general-purpose Git publishing tool.

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

The final report includes per-command pass/fail status, test counts when available, failure classifications, bounded stdout/stderr for failed checks, independent-review findings, and deterministic recommendations.

A code modification invalidates prior successful verification for the old revision.

## Outcome contract

### `VERIFIED`

Used only when the current implementation has sufficient acceptance evidence, applicable verification passes, no known new regression remains, scope is acceptable, and independent review approves the final diff.

Only `VERIFIED` results are eligible for opt-in branch publishing.

### `PARTIALLY_VERIFIED`

Used when implementation evidence exists but meaningful verification cannot be completed, commonly because of environmental, hardware, VPN, credential, or external-service limitations.

### `BLOCKED`

Used when DevAgent cannot safely understand, implement, or verify the task.

DevAgent is intentionally conservative: a truthful `BLOCKED` is better than a false `VERIFIED`.

## Safety boundary

DevAgent uses defense-in-depth controls around repository modification, command execution, and branch publishing.

- Clean Git repositories use a retained detached worktree by default.
- Pre-existing dirty developer files are protected.
- Existing files are backed up before their first modification.
- Workspace paths are confined and checked against symlink escape.
- Secret-like paths such as `.env*`, private keys, SSH/AWS credentials, and generated dependency trees are excluded from automatic reads.
- Engineering commands run as argv without a shell.
- Credential environment variables are scrubbed from verification subprocesses where appropriate.
- The model-facing command policy continues to block Git write operations.
- Optional publication is a separate deterministic post-verification step and requires explicit `--publish` or `--publish-branch` intent.
- Publication refuses protected/existing target branches and stages only reviewed changed paths.
- DevAgent never creates PRs, merges, rebases, force-pushes, or deploys.

DevAgent is **not** an operating-system sandbox. Always review the final report and pushed branch before integrating changes.

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
Add multiplication support and regression tests.

ROOT CAUSE
The calculator does not provide multiplication behavior.

IMPLEMENTATION
- Added multiply(a, b)
- Added multiplication regression coverage

FILES CHANGED
calculator.py
test_calculator.py

VERIFICATION SUMMARY
Passed checks: 4
Failed checks: 0

VERIFICATION RESULTS
✓ python -m pytest -q | phase=final | exit=0 | 0.15s | tests=5/5
✓ git diff --check | phase=final | exit=0 | 0.00s

INDEPENDENT REVIEW
APPROVED
The implementation is minimal and verified.

NEW REGRESSIONS
0

SOURCE CONTROL
Remote: origin
Branch: feature/devagent-multiplication
Commit: <sha>
Committed: YES
Pushed: YES
Pull request: NOT CREATED
Merge: NOT PERFORMED

DEVELOPER ACTION
Review remote branch: origin/feature/devagent-multiplication
Create a PR or merge only after developer review, if desired.
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

The machine-readable `report.json` also records the requested publication branch/remote, commit SHA, commit status, push status, and any publication error.

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

Automated tests use a deterministic fake provider and do not consume cloud-model credits. Source-control publication tests use a local bare Git repository rather than a network remote.

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

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. In particular, changes to DevAgent's safety, verification, or bounded publication behavior should include regression tests and must not give model-generated actions unrestricted Git publishing authority.

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
