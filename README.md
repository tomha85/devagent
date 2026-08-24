# DevAgent

DevAgent is a local, evidence-driven software engineering agent. Give it a repository and an engineering requirement; it discovers the application, compiles acceptance criteria, establishes a bounded plan, makes a backed-up minimal patch, runs repository-supported verification, reviews the final diff independently, and reports one of `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED`.

The model reasons inside individual lifecycle states. Python code—not model prose—owns transitions, path and command safety, modification tracking, verification validity, retry bounds, review enforcement, and the final status.

## Ownership and licensing

DevAgent was created and is maintained by **Tom Ha**.

Original project: https://github.com/tomha85/devagent

Copyright © 2026 Tom Ha. All rights reserved.

This repository is **source-available, not open source**. Use, modification, redistribution, commercial use, hosted-service use, and attribution requirements are governed by the proprietary terms in [LICENSE](LICENSE). Copies or authorized modifications must retain the applicable copyright, ownership, attribution, LICENSE, and NOTICE information.

See [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT) for attribution and ownership information.

## Install

```bash
pipx install git+https://github.com/tomha85/devagent.git
# or, from a checkout
pip install .
```

Development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Configure a provider

```bash
devagent setup --provider openai --model gpt-5
export OPENAI_API_KEY=...
```

Supported providers are OpenAI, Anthropic/Claude, xAI/Grok, and OpenAI-compatible local endpoints. For a local server:

```bash
devagent setup --provider compatible \
  --model local-model \
  --base-url http://127.0.0.1:11434/v1
```

Configuration stores the provider, model, endpoint, and API-key environment-variable name. It does not store the key itself.

## Use

From the application repository:

```bash
devagent "Fix websocket reconnect bug and add regression tests"
devagent "Add CSV export to reports"
pytest 2>&1 | devagent
devagent --input error.log
devagent                         # interactive when attached to a terminal
```

Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent status
```

Normal output is quiet apart from the final engineering report. `--verbose` shows lifecycle states, not hidden chain-of-thought.

## Deterministic lifecycle

```text
PREFLIGHT → DISCOVER → UNDERSTAND → TASK_SPEC → BASELINE → PLAN
→ GATHER_CONTEXT → REPRODUCE → IMPLEMENT → VERIFY_TARGETED
→ DIAGNOSE/REPLAN when needed → VERIFY_BROAD → REVIEW
→ QUALITY_CHECK → FINAL_VERIFY → LEARN → REPORT
```

The evidence gate rejects implementation unless the affected path, likely root cause, expected behavior, source evidence, proposed minimal solution, and confidence are present. Verification commands must derive from discovered repository capabilities. A modification increments the workspace revision and invalidates earlier successful verification.

## Repository intelligence

Discovery builds an evidence-backed repository/component model from source languages, manifests, package scripts, test locations, and CI workflow commands. It supports multi-component repositories and recognizes Python, JavaScript/TypeScript, Go, Rust, Java/Gradle/Maven, C/CMake, .NET, Make, and related capabilities without requiring one parser.

Context retrieval starts with task terms, filenames, exact text matches, test locations, and bounded source snippets. It does not upload the whole repository.

## Safety boundary

- Clean Git repositories run in a retained detached local worktree under `.devagent/worktrees/<run-id>/` by default. The report prints the exact review path.
- Dirty repositories use a conservative in-place fallback; pre-existing dirty files cannot be overwritten.
- Every existing file is copied once to `.devagent/runs/<run-id>/backups/` before its first edit.
- Resolved paths cannot escape the workspace, including through symlinks.
- `.env*`, keys, credentials, secrets, SSH, AWS, generated, VCS, and dependency directories are excluded from automatic reads.
- Commands execute as argv without a shell and with a credential-scrubbed environment and isolated `HOME`.
- Publishing, destructive Git, deletion, privilege, network-transfer, inline-interpreter, and package-install commands are blocked.
- DevAgent never commits, pushes, merges, rebases, or deploys.

DevAgent is defense in depth, not an operating-system sandbox. Review the report and diff before committing.

## Verification and review

Verification is adaptive: baseline, targeted tests, broader component checks, builds, lint/type checks, `git diff --check`, and final current-revision reruns where repository evidence supports them. Results store argv, exit code, duration, output, classification, phase, and revision. Success is based on exit codes—not words such as “passed.”

The independent reviewer receives the requirement, acceptance criteria, conventions, bounded final diff (including newly created files), tests, and process results. A rejection returns to implementation within a small correction budget, followed by required re-verification and a fresh review.

## Local run data and memory

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

Repository facts include evidence-file SHA-256 fingerprints and are discarded when evidence changes. Strategy memory is bounded to concrete repository commands and their sources. DevAgent never modifies its own installed source during an application task.

## Evaluation

Tests use `ScriptedFakeProvider`; automated runs never spend API credits. The suite covers path/secret/command safety, dirty work, backup ordering, discovery and monorepos, task and acceptance compilation, exit-code truth, failure classification, verification invalidation, review rejection, memory invalidation, CLI behavior, and a disposable division-by-zero end-to-end repository. The discovery matrix includes Python, TypeScript, Node, React-style, Go, Rust, C++, Java, and a monorepo. `devagent.evaluation.evaluate` records outcome, acceptance support, regressions, patch size, lifecycle iterations, model/tool calls, and runtime.

## Outcome contract

`VERIFIED` requires current-revision verification, applicable broader/static/build checks, accepted scope, acceptance evidence, clean diff validation, and reviewer approval. `PARTIALLY_VERIFIED` means implementation evidence exists but meaningful local verification is unavailable or failed for an environmental reason. `BLOCKED` means DevAgent cannot safely prove or complete the task.

The developer retains the final source-control decision.
