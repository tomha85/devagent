# DevAgent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Beta](https://img.shields.io/badge/status-beta-blue.svg)](#project-status)
[![Production CI](https://github.com/tomha85/devagent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomha85/devagent/actions/workflows/ci.yml)

**A local, evidence-driven software engineering agent that turns a requirement into a tested, independently reviewed patch, prints a developer-grade engineering report, and only publishes a branch when the result is `VERIFIED`.**

> **From requirement to evidence-backed verified branch.**

DevAgent runs against a developer's local repository. It discovers the application, gathers source evidence, compiles explicit acceptance criteria, plans a bounded change, creates backups, implements the minimum necessary patch, runs repository-supported verification, independently reviews the final diff, and decides `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED` from evidence rather than model confidence.

For a normal verified run the deterministic harness prints the complete engineering report first, then commits reviewed paths and fast-forward pushes the developer's current non-protected branch. If the developer starts on `main`, `master`, or `trunk`, DevAgent creates a new safe branch instead. Runtime DevAgent never creates a pull request, merges, rebases, force-pushes, or deploys.

## Why DevAgent?

Many coding agents optimize first for code generation. DevAgent is built around a different question:

**Can this change be supported by repository evidence and verified on the current revision?**

Core principles:

- **Evidence before modification** — insufficient source evidence blocks implementation.
- **Explicit acceptance contracts** — required criteria are `SATISFIED`, `UNPROVEN`, or `CONTRADICTED`; passing tests do not blanket-prove unrelated requirements.
- **False-`VERIFIED` resistance** — unsupported required criteria, missing final verification, known regressions, failed review, or source-state violations prevent a trustworthy success result.
- **Minimal-change discipline** — prefer the smallest correct diff over broad speculative refactors.
- **Backups before edits** — existing files are backed up before first modification.
- **Local-first isolation** — clean repositories use retained external detached worktrees by default; dirty developer work is protected.
- **Repository-native verification** — manifests, package scripts, test layouts, build files, and bounded CI evidence determine verification capabilities.
- **Independent review** — the final diff is reviewed separately from implementation.
- **Developer-grade reporting** — implementation logic, symbols, tests, acceptance evidence, verification, failures, gaps, and source-control status are recorded before publication.
- **Bring your own model** — OpenAI, Anthropic/Claude, xAI/Grok, Google Gemini, and OpenAI-compatible endpoints use one engineering workflow.
- **Deterministic branch publication** — only a `VERIFIED` isolated run can be committed and normally fast-forward pushed; `--no-publish` keeps the result local.

## Install

From PyPI:

```bash
python -m pip install devagent-ai
```

For an isolated CLI installation:

```bash
pipx install devagent-ai
```

For development from source:

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

The PyPI distribution is `devagent-ai`; the Python package and CLI command are both `devagent`.

## Configure a provider

OpenAI:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor
```

Anthropic / Claude:

```bash
devagent setup --provider anthropic --model YOUR_MODEL
export ANTHROPIC_API_KEY=...
devagent doctor
```

xAI / Grok:

```bash
devagent setup --provider xai --model YOUR_MODEL
export XAI_API_KEY=...
devagent doctor
```

Google Gemini:

```bash
devagent setup --provider gemini --model gemini-3.7-flash
export GEMINI_API_KEY=...
devagent doctor
```

Gemini uses Google's documented OpenAI-compatible Gemini endpoint so DevAgent can reuse the same bounded provider interface and deterministic local schema validation. `--provider google` is an alias for the same integration.

OpenAI-compatible local or private endpoint:

```bash
devagent setup \
  --provider compatible \
  --model local-model \
  --base-url http://127.0.0.1:11434/v1
```

DevAgent stores the provider configuration and the **name** of the API-key environment variable. It does not store the API key itself.

### Model routing by engineering role

DevAgent can use one default model or route different models to stable roles:

```text
investigator → understand repository/problem
planner      → produce bounded implementation/verification plan
implementer  → implement, diagnose, repair/replan
reviewer     → independently review final diff
```

The deterministic harness remains responsible for safety, tool execution, verification validity, acceptance adjudication, final status, reporting, and source-control publication regardless of which model handles a role.

## Run

From the application repository:

```bash
devagent "Fix websocket reconnect bug and add regression tests"
```

For a normal `VERIFIED` result:

```text
DISCOVER / UNDERSTAND
        ↓
COMPILE ACCEPTANCE CONTRACT
        ↓
BASELINE / PLAN / GATHER CONTEXT
        ↓
IMPLEMENT MINIMAL PATCH
        ↓
TARGETED + BROAD VERIFICATION
        ↓
DIAGNOSE / REPLAN if needed
        ↓
INDEPENDENT REVIEW
        ↓
FINAL CURRENT-REVISION VERIFICATION
        ↓
FULL ENGINEERING REPORT
        ↓
VERIFIED ONLY: COMMIT + FAST-FORWARD PUSH BRANCH
        ↓
STOP — NO PR, NO MERGE
```

Repeated prompts on a normal local development branch continue that same branch when local/remote history is safely compatible. A protected starting branch (`main`, `master`, `trunk`) causes a new branch such as:

```text
devagent/20260825T020000Z-ab12cd
```

Explicitly start a new branch:

```bash
devagent \
  --publish-branch feature/devagent-csv-export \
  "Add CSV export to reports"
```

Run without commit/push:

```bash
devagent --no-publish "Add CSV export to reports"
```

Long requirements can be read from any bounded UTF-8 text file path; the filename extension is unrestricted:

```bash
devagent --input requirements/customer-feature.md
devagent --input ./task
devagent --input ../specs/release.requirement
```

Binary data, invalid UTF-8, secret-like paths, and files above the input-size bound are refused.

Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent models
devagent status
```

## Outcome contract

### `VERIFIED`

Used only when required acceptance criteria are satisfied by admissible evidence, applicable final verification passes on the current revision, no known new regression remains, scope is acceptable, and independent review approves the final diff.

Only `VERIFIED` is eligible for automatic commit/push.

### `PARTIALLY_VERIFIED`

Used when meaningful implementation evidence exists but complete proof cannot be obtained, for example because of unavailable hardware, VPNs, credentials, external services, or environment limitations.

It is never auto-published.

### `BLOCKED`

Used when DevAgent cannot safely understand, implement, or verify the request. It is never auto-published.

A truthful conservative result is preferable to a false `VERIFIED`.

## Safety boundary

DevAgent uses defense-in-depth controls around repository modification, command execution, and publishing:

- external isolated worktrees for clean repositories by default;
- dirty tracked and real untracked developer files are protected;
- backups before first file modification;
- workspace confinement and symlink-escape checks;
- secret-like path exclusions;
- engineering commands executed as argv without a general shell;
- credential environment scrubbing for verification where appropriate;
- model-facing command policy blocks Git write operations;
- publication is a separate deterministic post-report step;
- only reviewed changed paths are staged;
- remote branch state is captured and rechecked to block publication races;
- protected targets are refused and force push is never used;
- no runtime PR, merge, rebase, force-push, or deployment automation.

DevAgent is **not an operating-system sandbox**. Review the report and pushed branch before integrating customer or production code.

## Repository intelligence and verification

Discovery understands common evidence from:

- Python: `pyproject.toml`, requirements, pytest/unittest conventions;
- JavaScript/TypeScript: `package.json`, scripts, TypeScript configuration, React/Vite/Next-style repositories;
- Go: `go.mod`;
- Rust: `Cargo.toml`;
- Java: Maven/Gradle;
- C/C++: CMake/Make/Meson;
- .NET: solution/project files;
- CI: GitHub Actions, Jenkins, GitLab CI, Azure Pipelines;
- multi-component repositories and repository documentation.

Verification can include baseline tests, targeted tests, component/broad checks, integration/e2e commands, builds, lint/type checks, and `git diff --check`. Every verification result records phase and revision so an edit invalidates success from an older tree.

## Production qualification

DevAgent 0.4.0 adds **production qualification v3**. The release catalog contains 50 required cases and preserves the primary invariant:

```text
false_verified == 0
```

It covers end-to-end engineering behavior, acceptance truthfulness, task scope, provider contracts/parity, model routing, worktree and Git publication safety, CLI input, review/repair loops, report/evaluation integrity, release integrity, and actual repository-native toolchain execution for:

```text
Python / pytest
Node + TypeScript repository discovery
Go
Rust / Cargo
C++ / Make
```

Run the release qualification locally on a machine with those toolchains:

```bash
python -m devagent.qualification \
  --catalog evaluation/benchmark_v3.json \
  --report .devagent/production-qualification-v3.json
```

Production CI runs Python 3.10/3.11/3.12, a clean wheel install, and this production qualification gate. The qualification JSON is retained as CI evidence.

**100% qualified means 100% of this explicit catalog passed.** It does not mean mathematical correctness for every unseen repository, environment, model response, language, or engineering task.

See [docs/production-readiness.md](docs/production-readiness.md) for the evidence behind the 0.4.0 production-readiness assessment and its explicit limitations.

## Provider architecture

Currently supported:

| Provider | Configuration |
| --- | --- |
| OpenAI | `--provider openai` |
| Anthropic / Claude | `--provider anthropic` or `--provider claude` |
| xAI / Grok | `--provider xai` or `--provider grok` |
| Google Gemini | `--provider gemini` or `--provider google` |
| Local / OpenAI-compatible | `--provider compatible --base-url ...` |

Provider choice affects reasoning quality, cost, latency, and privacy characteristics. It does not change DevAgent's deterministic acceptance, safety, verification, reporting, and publication rules.

## Engineering report

The report is emitted **before** source-control publication and includes:

- implementation logic summary first;
- requirement, task type, and risk;
- root cause or design gap;
- implementation decisions;
- deterministically extractable changed symbols and test cases;
- acceptance criteria with status, evidence, and reasons;
- verification matrix with command, phase, revision, exit code, duration, and test counts;
- failed-check output and failure classification;
- independent-review result;
- completeness and known gaps;
- recommendations;
- source-control plan/status and developer review checklist.

After a successful verified publication, a separate receipt records remote, branch, exact commit SHA, committed/pushed status, and confirms no PR or merge was performed.

## Local run data

Run artifacts are stored under the target repository's `.devagent/` state, including metadata, per-run backups, observations, verification records, `report.json`, retained worktrees, and evidence-backed repository/strategy memory.

Repository facts carry source fingerprints so stale evidence can be invalidated when source files change.

## Development

```bash
python -m compileall -q devagent
pytest -q
git diff --check
python -m devagent.qualification
```

Automated provider tests normally use deterministic or mocked clients and do not consume cloud-model credits. Source-control tests use disposable local Git repositories. Production qualification deliberately executes its real local toolchain fixtures.

## Project status

DevAgent 0.4 is **beta software** with a production-readiness target of approximately **9/10 for the documented local engineering workflow**. That assessment is based on explicit qualification evidence, not on a claim of universal correctness or parity with every capability of a hosted coding platform.

Remaining gaps include browser/UI runtime qualification, a broader Java/.NET/database-migration matrix, very large monorepo benchmarks, parallel multi-agent orchestration, operating-system sandboxing, and continuous paid real-provider testing across every model/provider combination.

The project intentionally prioritizes trustworthy outcomes over feature count.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Changes to safety, verification, reporting, acceptance semantics, providers, or bounded publication behavior should include regression evidence and must not give model-generated actions unrestricted Git publishing authority.

## Security

Please do not publish sensitive vulnerability details in a public issue. See [SECURITY.md](SECURITY.md) for the reporting process and security scope.

## License and attribution

DevAgent is open source under the [MIT License](LICENSE).

```text
Copyright (c) 2026 Tom Ha
```

DevAgent was created by **Tom Ha**. Original repository: **https://github.com/tomha85/devagent**. See [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT) for project attribution.
