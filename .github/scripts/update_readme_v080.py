from pathlib import Path

path = Path("README.md")
text = path.read_text(encoding="utf-8")

text = text.replace(
    "DevAgent 0.5 adds an opt-in benchmark runner for pinned GitHub repositories.",
    "DevAgent includes an opt-in benchmark runner for pinned GitHub repositories.",
)

old_sandbox = (
    "DevAgent is **not an operating-system sandbox**. Review the report and pushed branch before integrating customer or production code."
)
new_sandbox = (
    "On Linux, DevAgent can execute engineering commands inside a bubblewrap-based operating-system sandbox. "
    "Production qualification exercises required sandbox mode with network access denied. Required mode fails closed "
    "when isolation cannot be established rather than silently falling back. Review the report and pushed branch before "
    "integrating customer or production code: sandboxing reduces execution risk, but it does not make arbitrary generated "
    "changes universally safe."
)
if old_sandbox not in text:
    raise SystemExit("expected legacy sandbox paragraph not found")
text = text.replace(old_sandbox, new_sandbox)

prod_start = text.index("## Production qualification\n")
prod_end = text.index("## Provider architecture\n", prod_start)
new_prod = '''## Production qualification

DevAgent 0.8.0 uses cumulative production qualification rather than replacing older evidence with a smaller new suite.

- **v4 — 70 required cases** covering end-to-end engineering behavior, acceptance truthfulness, task/risk scope, provider contracts and parity, model routing, worktree and Git publication safety, CLI input, review/repair loops, report/evaluation integrity, release integrity, large-repository behavior, structural refactors, Java/.NET discovery and execution, SQLite migration forward/rollback, and real repository-native stacks.
- **v5 — 9 required autonomy cases** covering bounded parallel coordination, dirty-source refusal, real isolated parallel DevAgent runs, bounded/relevant skills and provider injection, automation overlap claim/recovery, and provider-benchmark deduplication, live structured-contract behavior, and secret redaction.

The v0.8 merge commit on `main` passed both catalogs in required Linux sandbox mode:

```text
v4: 70/70 passed
v5:  9/9 passed
combined: 79/79 passed
```

The qualification environment exercises real local toolchains for:

```text
Python / pytest
Node + TypeScript repository discovery
Go
Rust / Cargo
C++ / Make
Java / Maven
.NET build
SQLite migration forward + rollback
```

Run the same release qualification catalogs locally on a machine with the required toolchains:

```bash
DEVAGENT_SANDBOX=required DEVAGENT_NETWORK=deny \\
python -m devagent.qualification \\
  --catalog evaluation/benchmark_v4.json \\
  --report .devagent/production-qualification-v4.json

DEVAGENT_SANDBOX=required DEVAGENT_NETWORK=deny \\
python -m devagent.qualification \\
  --catalog evaluation/benchmark_v5.json \\
  --report .devagent/production-qualification-v5.json
```

Production CI also runs Python 3.10/3.11/3.12, a clean wheel build/install, real bubblewrap sandbox smoke, and both qualification catalogs. Qualification JSON is retained as CI evidence.

**100% qualified means 100% of these explicit catalogs passed on that revision and environment.** It does not mean mathematical correctness for every unseen repository, environment, model response, language, provider, or engineering task, and it is not a claim that DevAgent is universally superior to every hosted coding platform.

See [docs/production-readiness.md](docs/production-readiness.md) for the project's earlier readiness assessment and its explicit limitations.

'''
text = text[:prod_start] + new_prod + text[prod_end:]

status_start = text.index("## Project status\n")
status_end = text.index("## Contributing\n", status_start)
new_status = '''## Project status

DevAgent 0.8.0 is **beta software with a verified core release baseline**. The exact v0.8 merge revision on `main` passed Production CI across Python 3.10/3.11/3.12, clean wheel installation, real Linux bubblewrap sandbox execution, production qualification v4 (**70/70**), and autonomy qualification v5 (**9/9**), for **79/79 cumulative required qualification cases**.

The current core includes evidence-backed `VERIFIED` / `PARTIALLY_VERIFIED` / `BLOCKED` outcomes, backup-first editing, isolated worktrees, bounded structural file operations, repository-native verification, independent review, safe branch publication, provider/model choice, Java and .NET engineering discovery/execution, SQLite migration forward/rollback verification, large-monorepo deep-manifest discovery, bounded parallel agents, repository-local skills, foreground automations, Linux OS sandboxing, bounded browser/local-UI verification, and real-provider structured-contract benchmarking.

These results are **bounded engineering claims**, not universal-correctness or market-superiority claims. They are tied to explicit qualification cases, pinned revisions, deterministic fixtures/external oracles where applicable, and the environments actually exercised by CI.

Remaining work is primarily **breadth and external validation**, not missing core architecture: a larger public corpus of pinned upstream repositories and tasks; broader browser/UI coverage across dynamic applications and multiple browser environments; a wider Java/Gradle, .NET test-framework, and PostgreSQL/MySQL migration matrix beyond the current qualified fixtures; larger and more diverse monorepo stress cases beyond the current >12,000-file deep-manifest case; more real-world multi-agent workload studies; and continuous paid real-provider benchmarking across a broader set of model/provider combinations. GitHub branch protection/rulesets are external repository settings and must be configured separately; DevAgent does not claim to configure them itself.

The project intentionally prioritizes trustworthy outcomes, reproducible evidence, and safe engineering behavior over feature count or unsupported "best agent" claims.

'''
text = text[:status_start] + new_status + text[status_end:]

path.write_text(text, encoding="utf-8")
