# DevAgent 0.4.0 production-readiness evidence

DevAgent 0.4.0 targets a **9/10 production-readiness level for its documented local engineering workflow**. That number is an engineering assessment, not a mathematical guarantee or a claim of universal parity with any other coding agent.

The release decision is evidence-based. A release candidate must satisfy all of the following gates:

- the primary truthfulness invariant remains `false_verified == 0` for the explicitly evaluated scenarios;
- all required acceptance criteria use explicit `SATISFIED` / `UNPROVEN` / `CONTRADICTED` semantics rather than non-empty evidence as a proxy;
- Production CI passes on Python 3.10, 3.11, and 3.12;
- the package builds and installs from a clean wheel environment;
- production qualification v3 passes every catalog case;
- actual repository-native test/build commands execute for the qualified Python, Node/TypeScript, Go, Rust, and C++ fixtures;
- protected-branch behavior, dirty-work protection, worktree isolation, remote-race checks, and non-`VERIFIED` publication refusal remain green;
- provider contracts and model-role routing remain green;
- version metadata, exact-main release targeting, exact-tag package build, and PyPI Trusted Publishing checks remain green.

## What 9/10 means here

The score reflects a strong local engineering harness with deterministic safety and verification around model reasoning:

| Area | Release evidence |
| --- | --- |
| Truthfulness / false `VERIFIED` resistance | Required acceptance-status gate, baseline-preservation checks, final verification, independent review, regression/source-state evaluation |
| Repository safety | External isolated worktrees, dirty-file protection, path/symlink/secret controls, bounded command execution |
| Source-control safety | Reviewed-path staging, protected-target refusal, normal fast-forward publishing only, remote race detection, no runtime PR/merge/rebase/force-push/deploy |
| Provider portability | OpenAI, Anthropic/Claude, xAI/Grok, Google Gemini, and OpenAI-compatible endpoints behind one role-routing contract |
| Verification portability | Executable production fixtures for Python/pytest, Node/TypeScript, Go, Rust/Cargo, and C++/Make |
| Packaging/release | Multi-Python CI, coverage gate, clean wheel installation, exact verified-SHA release, exact-tag build, Trusted Publishing |
| Reporting | Developer-grade report before publication plus machine-readable run and qualification evidence |

## Why it is not 10/10

Important production gaps remain outside the 0.4.0 qualification claim:

- no operating-system or cloud sandbox boundary;
- no browser/UI runtime automation qualification yet;
- no broad Java/.NET/database-migration real-toolchain matrix yet;
- no large-scale monorepo or million-file benchmark;
- no parallel multi-agent orchestration;
- paid real-provider behavior is contract-tested deterministically but not continuously exercised across every provider/model in CI;
- external systems such as VPNs, proprietary build infrastructure, hardware, credentials, and customer services can still prevent complete verification.

These gaps should produce `PARTIALLY_VERIFIED` or `BLOCKED` when they prevent trustworthy proof. They must not be hidden behind a `VERIFIED` result.

## Relationship to hosted coding agents

DevAgent is not designed to clone another product. Its competitive focus is the **evidence-backed local verification harness**: the developer chooses the model, while deterministic code controls repository evidence, acceptance criteria, modifications, verification, review, final status, reporting, and bounded branch publication.

A stronger model can improve investigation, planning, implementation, diagnosis, and review quality. It cannot bypass DevAgent's deterministic evidence and release gates.

## Release qualification command

```bash
python -m devagent.qualification \
  --catalog evaluation/benchmark_v3.json \
  --report .devagent/production-qualification-v3.json
```

A release is considered fully qualified only when the report says every catalog case passed. The phrase **100% qualified** always refers to this finite explicit catalog, never to every possible software-engineering task.
