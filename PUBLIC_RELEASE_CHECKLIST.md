# DevAgent Public Release Checklist

Use this checklist before changing repository visibility from private to public and before publishing a release that materially expands the public asset boundary.

A public repository exposes more than the current working tree. Git history, branches, tags, releases, issue/PR content, and committed artifacts must be treated as part of the disclosure surface.

## 1. Exact release baseline

- Record the exact `main` commit SHA intended for public release.
- Confirm the working release branch is derived from that exact baseline.
- Preserve a private backup/reference before any public-release cleanup that rewrites or removes content.
- Confirm version metadata, documentation, and release claims refer to the correct revision.

## 2. Full-history secret scan

Scan the complete Git history, not only the current tree, for:

- API keys and tokens;
- passwords and connection strings;
- private keys and certificate bundles;
- cloud, registry, CI, or deployment credentials;
- customer endpoint credentials;
- personally identifying or confidential customer data.

If a real secret ever entered Git history, revoke/rotate the credential even if the file was later deleted. Removing a secret from the latest commit is not equivalent to revoking it.

## 3. Customer and field-data audit

Confirm no public branch, tag, release artifact, fixture, issue, PR, or history entry contains unauthorized:

- PLC project exports;
- requirements/specifications;
- customer reports;
- OPC UA endpoints, namespaces, mappings, runtime captures, or site topology;
- field failure/incident evidence;
- screenshots containing customer names, addresses, equipment IDs, or proprietary logic;
- private evidence history or commissioning records.

## 4. Vendor and third-party licensing audit

For every non-trivial fixture or sample, confirm that redistribution is permitted.

Do not publish vendor software, libraries, project exports, manuals, sample projects, or generated artifacts when their license does not permit redistribution.

Prefer synthetic, independently authored, public-domain, or permissively licensed fixtures with documented provenance.

## 5. Open-source/commercial boundary

- Review [OPEN_SOURCE.md](OPEN_SOURCE.md).
- Review [COMMERCIAL.md](COMMERCIAL.md).
- Confirm private qualification corpora and commercial compatibility intelligence remain outside the public repository.
- Confirm no customer-specific semantic/rule pack is accidentally embedded in public fixtures or documentation.
- Confirm public qualification claims are bounded to the actual published cases and evidence.

## 6. Trademark and project identity

- Review [TRADEMARKS.md](TRADEMARKS.md).
- Confirm the official repository, PyPI package, documentation, and project identity are consistent.
- Confirm modified third-party forks are not being represented as official project releases.

## 7. Security and contribution policy

- Review [SECURITY.md](SECURITY.md).
- Review [CONTRIBUTING.md](CONTRIBUTING.md).
- Confirm private vulnerability reporting is configured if available.
- Confirm contribution rules prohibit customer, employer, vendor, and confidential third-party material without redistribution rights.

## 8. Repository governance

Before public release, configure repository settings appropriate for the project, including where available:

- branch protection/rulesets for `main`;
- no force-push or branch deletion for protected release branches;
- required reviews and/or required status checks once CI is executing reliably;
- protected release/tag workflow;
- least-privilege collaborator permissions;
- security scanning and dependency alerts appropriate to the repository.

Do not represent a status check as passing when the workflow did not actually execute its required steps.

## 9. CI and release evidence

- Confirm the intended CI jobs actually execute rather than ending before steps start.
- Confirm tests/qualification reported as PASS ran on the exact revision claimed.
- Confirm package build/install evidence refers to the same release SHA/tag.
- Keep simulator/static qualification distinct from real vendor endpoint or field certification.

## 10. Visibility change

Only after the preceding checks are complete:

1. confirm the exact commit intended to become public;
2. confirm the public/private asset boundary one final time;
3. change repository visibility to public using an authorized GitHub repository-setting action;
4. verify the repository is publicly reachable without authentication;
5. inspect the public repository as an unauthenticated user;
6. verify README, license, security, trademark, contribution, and commercial-boundary documents render correctly.

## 11. Post-public verification

Immediately after publication:

- verify repository visibility is `public`;
- verify default branch and release tags are correct;
- verify no unintended branch or artifact became public;
- verify PyPI/project links resolve to the official repository;
- verify no customer/private material appears in code search;
- record the first public baseline SHA for future audit purposes.

A visibility change should be treated as a release event, not as a cosmetic repository setting.
