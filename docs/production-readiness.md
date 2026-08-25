# DevAgent 0.5.1 production-readiness evidence

DevAgent 0.5.1 targets a **9/10 production-readiness level for its documented local single-agent engineering workflow**. That number is a bounded engineering assessment, not a mathematical guarantee, a claim of universal correctness, or a claim of feature parity with every hosted coding platform.

The release decision is evidence-based. A release candidate must satisfy all of the following gates:

- the primary truthfulness invariant remains `false_verified == 0` for the explicitly evaluated scenarios;
- all required acceptance criteria use explicit `SATISFIED` / `UNPROVEN` / `CONTRADICTED` semantics rather than non-empty evidence as a proxy;
- Production CI passes on Python 3.10, 3.11, and 3.12;
- the package builds and installs from a clean wheel environment;
- Production Qualification v4 passes every catalog case;
- actual repository-native test/build commands execute for the qualified Python, Node/TypeScript, Go, Rust, and C++ fixtures;
- protected-target behavior, dirty-work protection, worktree isolation, remote-race checks, and non-`VERIFIED` publication refusal remain green;
- provider contracts and model-role routing remain green;
- explicit provider switching cannot inherit a stale base URL from a different configured provider;
- `devagent doctor --live` can explicitly probe the configured real provider/model structured-output path and returns non-zero when that probe fails;
- repository-controlled Git hooks are disabled during deterministic VERIFIED commit/push publication;
- provider adapter status is reported conservatively as `CONTRACT-QUALIFIED`, `SUPPORTED`, `TEST-ONLY`, or `EXPERIMENTAL`; adapter qualification is not presented as proof that every paid model is currently reachable;
- version metadata, exact-main release targeting, exact-tag package build, and PyPI Trusted Publishing checks remain green.

## What 9/10 means here

The score reflects a strong local engineering harness with deterministic safety and verification around model reasoning:

| Area | Release evidence |
| --- | --- |
| Truthfulness / false `VERIFIED` resistance | Required acceptance-status gate, baseline-preservation checks, final verification, independent review, regression/source-state evaluation |
| Repository safety | External isolated worktrees, dirty-file protection, path/symlink/secret controls, bounded command execution |
| Source-control safety | Reviewed-path staging, protected-target refusal, repository Git hooks disabled for commit/push, normal fast-forward publishing only, remote race detection, no runtime PR/merge/rebase/force-push/deploy |
| Provider portability | OpenAI, Anthropic/Claude, xAI/Grok, Google Gemini, and OpenAI-compatible endpoints behind one role-routing contract |
| Provider trust | Conservative adapter qualification labels plus opt-in live structured-output readiness probes for the default and configured role models |
| Verification portability | Executable production fixtures for Python/pytest, Node/TypeScript, Go, Rust/Cargo, and C++/Make |
| Large-repository behavior | Bounded inventory/retrieval, generated/vendor-tree pruning, and Git-backed retrieval recovery beyond the scan frontier |
| Benchmark truthfulness | Pinned public-repository benchmark contract, deterministic mutations, independent baseline/final oracles, and explicit false-`VERIFIED` detection |
| Packaging/release | Multi-Python CI, coverage gate, clean wheel installation, exact verified-SHA release, exact-tag build, Trusted Publishing |
| Reporting | Developer-grade report before publication plus machine-readable run and qualification evidence |

## Why it is not 10/10

Important production/platform gaps remain outside the 0.5.1 qualification claim:

- no operating-system or cloud sandbox boundary yet;
- no browser/UI runtime automation qualification yet;
- no broad Java/.NET/database-migration full-lifecycle real-toolchain matrix yet;
- no very-large monorepo or million-file stress benchmark above the current bounded retrieval qualification;
- no parallel multi-agent orchestration yet;
- no large published corpus of pinned upstream real-world benchmark cases yet;
- paid real-provider behavior is contract-tested deterministically and can be checked on demand with `doctor --live`, but is not continuously exercised across every supported provider/model combination in CI;
- external systems such as VPNs, proprietary build infrastructure, hardware, credentials, and customer services can still prevent complete verification.

These gaps should produce `PARTIALLY_VERIFIED` or `BLOCKED` when they prevent trustworthy proof. They must not be hidden behind a `VERIFIED` result.

## Provider qualification language

DevAgent deliberately separates three different claims:

- **Supported** means an adapter/configuration path exists.
- **Contract-qualified** means deterministic DevAgent provider-contract tests cover that adapter behavior.
- **Live pass** means the user's currently configured provider/model/key successfully completed the opt-in structured-output probe during `devagent doctor --live`.

A live pass is point-in-time readiness evidence, not a permanent quality score. Future real-provider benchmark work will add task-success, false-`VERIFIED`, false-blocked, runtime, and cost measurements for named models.

## Relationship to hosted coding agents

DevAgent is not designed to clone another product. Its competitive focus is the **evidence-backed model-neutral local verification harness**: the developer chooses the model, while deterministic code controls repository evidence, acceptance criteria, modifications, verification, review, final status, reporting, and bounded branch publication.

A stronger model can improve investigation, planning, implementation, diagnosis, and review quality. It cannot bypass DevAgent's deterministic evidence and release gates.

## Release qualification command

```bash
python -m devagent.qualification \
  --catalog evaluation/benchmark_v4.json \
  --report .devagent/production-qualification-v4.json
```

A release is considered fully qualified only when the report says every catalog case passed. The phrase **100% qualified** always refers to this finite explicit catalog, never to every possible software-engineering task.
