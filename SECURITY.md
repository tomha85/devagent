# Security Policy

DevAgent modifies source code and executes repository-supported verification commands, so security boundaries are part of the product's core behavior.

## Supported versions

During the alpha phase, security fixes target the latest code on `main` and the latest published release when applicable.

Older development snapshots may not receive security fixes.

## Reporting a vulnerability

Please do **not** open a public issue containing exploit details, credentials, private repository content, or other sensitive information.

Use GitHub's private vulnerability reporting / Security Advisory flow for this repository when available. If private reporting is not available, contact the maintainer through the GitHub profile for `tomha85` and coordinate a private disclosure channel before sending sensitive details.

Include, when possible:

- affected DevAgent version or commit,
- operating system and Python version,
- reproduction steps,
- expected versus observed behavior,
- security impact,
- whether the issue can escape the target workspace, expose secrets, run an unsafe command, modify protected developer work, publish to an unauthorized branch, or falsely report `VERIFIED`.

## High-priority security areas

Reports are especially important when they involve:

- workspace or symlink escape,
- secret or credential exposure,
- unsafe shell or subprocess execution,
- destructive Git operations,
- model-driven or non-opt-in commit/push behavior,
- publication to protected or pre-existing remote branches,
- pull-request, merge, rebase, force-push, or deploy automation,
- staging paths that were not part of the reviewed verified change,
- modification of pre-existing dirty developer files,
- bypass of backup-before-edit guarantees,
- malicious repository content influencing unsafe tool execution,
- provider output bypassing deterministic validation,
- false `VERIFIED` outcomes without supporting executed evidence.

## Security model

DevAgent uses defense-in-depth controls, including path confinement, sensitive-path exclusions, bounded command execution, protected dirty files, backups before edits, verification invalidation after modification, and a bounded publication boundary.

Engineering/model-facing command execution continues to block Git write operations. Optional publication is a separate deterministic post-verification path that requires explicit user intent, a `VERIFIED` outcome, the default isolated worktree, a new non-protected branch, and staging limited to reviewed changed paths. DevAgent does not create pull requests or perform merges, rebases, force pushes, or deployments.

These controls reduce risk but do not make DevAgent an operating-system sandbox. Run DevAgent only in environments and repositories you are prepared to inspect, and review the final report and pushed branch before integrating changes.

## Disclosure

Please allow reasonable time for investigation and remediation before public disclosure. Once a fix is available, maintainers may coordinate publication of the issue and remediation details with the reporter.
