# Security Policy

DevAgent modifies source code and executes repository-supported verification commands, and DevAgent Live consumes industrial runtime evidence, so security and evidence-integrity boundaries are part of the product's core behavior.

## Supported versions

During the public beta phase, security fixes target the latest code on `main` and the latest published release when applicable.

Older development snapshots may not receive security fixes.

## Reporting a vulnerability

Please do **not** open a public issue containing exploit details, credentials, private repository content, customer PLC logic, site information, runtime evidence, or other sensitive information.

Use GitHub's private vulnerability reporting / Security Advisory flow for this repository when available. If private reporting is not available, contact the maintainer through the GitHub profile for `tomha85` and coordinate a private disclosure channel before sending sensitive details.

Include, when possible:

- affected DevAgent version or commit,
- operating system and Python version,
- reproduction steps,
- expected versus observed behavior,
- security impact,
- whether the issue can escape the target workspace, expose secrets, run an unsafe command, modify protected developer work, publish to an unauthorized branch, violate the DevAgent Live read-only boundary, corrupt evidence integrity, or falsely report `VERIFIED`.

## High-priority security areas

Reports are especially important when they involve:

- workspace or symlink escape,
- secret or credential exposure,
- accidental disclosure of customer or confidential industrial data,
- unsafe shell or subprocess execution,
- destructive Git operations,
- publication before the engineering report is emitted,
- publication of a non-`VERIFIED` run,
- publication to protected, unexpected, or diverged remote branches,
- pull-request, merge, rebase, force-push, or deploy automation,
- staging paths that were not part of the reviewed verified change,
- modification of pre-existing dirty developer files,
- bypass of backup-before-edit guarantees,
- malicious repository content influencing unsafe tool execution,
- provider output bypassing deterministic validation,
- false `VERIFIED` outcomes without supporting executed evidence,
- PLC write/force/reset/bypass/download/mode-change/start-stop control being exposed through DevAgent Live,
- stale, replayed, ambiguous, incomplete, or discontinuous runtime evidence being incorrectly treated as trusted/current/complete,
- monitoring, reconnect, overflow, queue-loss, or history gaps being hidden from commissioning conclusions.

## Security model

DevAgent uses defense-in-depth controls, including path confinement, sensitive-path exclusions, bounded command execution, protected dirty files, backups before edits, verification invalidation after modification, and a bounded publication boundary.

Engineering/model-facing command execution continues to block Git write operations. For a normal isolated run, DevAgent prints the complete engineering review report first. Only after that report is emitted may the separate deterministic publication path commit and push a `VERIFIED` result. A current local non-protected development branch may be continued, but its remote HEAD is captured before model execution and checked again before publication; diverged or unexpectedly moved branches are blocked. When the developer is on `main`, `master`, or `trunk`, DevAgent creates a new safe branch instead of publishing to the protected branch. The publication path stages only reviewed changed paths and uses normal fast-forward push semantics without force push. Developers can disable publication with `--no-publish`.

DevAgent does not create pull requests or perform merges, rebases, force pushes, or deployments at runtime.

DevAgent PLC static/offline engineering review does not itself prove real-controller, simulator, HIL, field-wiring, or process behavior unless corresponding evidence is supplied. DevAgent Live remains read-only and must not expose PLC control authority to model reasoning.

These controls reduce risk but do not make arbitrary generated changes, external vendor software, customer networks, or industrial environments universally safe. Review the engineering report and resulting branch before integration, and preserve normal industrial change-control and commissioning procedures.

## Public repository disclosure boundary

Security review for public releases must include more than the latest working tree. Git history, branches, tags, release artifacts, issues, pull requests, and committed fixtures can expose sensitive information.

Before changing repository visibility or publishing a materially expanded public corpus, follow [PUBLIC_RELEASE_CHECKLIST.md](PUBLIC_RELEASE_CHECKLIST.md) and [OPEN_SOURCE.md](OPEN_SOURCE.md).

Customer data, private field evidence, confidential compatibility intelligence, credentials, and non-redistributable vendor artifacts must not be used as public fixtures merely because DevAgent core code is open source.

## Disclosure

Please allow reasonable time for investigation and remediation before public disclosure. Once a fix is available, maintainers may coordinate publication of the issue and remediation details with the reporter.
