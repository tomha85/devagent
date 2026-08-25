# DevAgent v0.6 Runtime and Sandbox

DevAgent v0.6 adds an explicit runtime boundary for verification commands and local UI checks.

## Default behavior

- `DEVAGENT_SANDBOX=auto` is the default.
- On Linux, DevAgent uses `bubblewrap` (`bwrap`) when it is installed.
- The host filesystem is mounted read-only and only the selected repository plus explicit per-run state are rebound writable.
- Engineering command networking defaults to `DEVAGENT_NETWORK=deny`.
- HOME and TMPDIR point at per-run DevAgent state so cloud credentials and normal package-manager state are not inherited.
- Existing token-aware command restrictions still apply inside the OS sandbox.

If `bwrap` is unavailable in `auto` mode, DevAgent falls back to its existing command/environment policy. Set `DEVAGENT_SANDBOX=required` when OS-level isolation is mandatory; the run fails closed if `bwrap` is unavailable. If `bwrap` exists but the host prevents the required namespace setup, DevAgent also fails closed instead of silently running with weaker isolation. `DEVAGENT_SANDBOX=off` is intended only for trusted development environments.

## Ubuntu 24.04+ AppArmor setup

Ubuntu 24.04 and newer can restrict unprivileged user namespaces through AppArmor. A common symptom when `DEVAGENT_NETWORK=deny` is:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

Do not work around this by globally disabling AppArmor or silently enabling unrestricted networking. A narrow host profile can grant the required `userns` permission only to the packaged bubblewrap binary:

```bash
sudo apt-get install -y bubblewrap apparmor-utils

sudo tee /etc/apparmor.d/bwrap >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
PROFILE

sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

Use the actual `bwrap` executable path if your distribution installs it somewhere other than `/usr/bin/bwrap`. DevAgent's Production Qualification configures this narrow profile on Ubuntu GitHub-hosted runners and then requires the real sandbox; the release gate does not replace network isolation with a proxy-only fallback.

## Controlled network

`DEVAGENT_NETWORK=deny` creates a private Linux network namespace for sandboxed commands. The command cannot use the host network. Set `DEVAGENT_NETWORK=inherit` only for a verification step that genuinely needs host networking, such as a local browser test or an explicitly approved dependency installation.

Network URLs, shell execution, destructive Git operations, SSH/SCP/curl/wget, inline interpreter evaluation, and secret paths remain blocked by the structured command policy.

## Safe dependency installation

Dependency installation remains disabled unless both of these are set:

```bash
export DEVAGENT_ALLOW_DEPENDENCY_INSTALL=1
export DEVAGENT_NETWORK=inherit
```

Even then, DevAgent accepts only bounded repository-driven forms:

- pip: `pip install -r <repository requirement file>` (or `python -m pip install -r ...`)
- npm: `npm ci`; DevAgent adds `--ignore-scripts`
- pnpm: `pnpm install`; DevAgent adds `--frozen-lockfile --ignore-scripts`
- yarn: `yarn install`; DevAgent adds a frozen/immutable lockfile constraint plus `--ignore-scripts`

Direct package names, editable installs, custom registries/indexes, direct URLs, and arbitrary package additions are rejected.

## Browser/UI verification

The `devagent-ui-check` command performs a bounded Chromium-family headless check:

```bash
devagent-ui-check dist/index.html --repo .
```

It accepts only repository files or localhost HTTP(S) URLs. External web targets are rejected. Localhost checks require `DEVAGENT_NETWORK=inherit`; the browser is additionally configured to route non-local HTTP(S) traffic to an unused loopback proxy so the UI check does not become a general browsing capability.

Example for a local development server:

```bash
export DEVAGENT_NETWORK=inherit
devagent-ui-check http://127.0.0.1:4173 --repo .
```

The verifier writes its latest screenshot under `.devagent/browser/latest.png` and returns a non-zero status on browser failure.

## Runtime modes

| Setting | Meaning |
| --- | --- |
| `DEVAGENT_SANDBOX=auto` | Use Linux bwrap when available; otherwise retain policy-only isolation. |
| `DEVAGENT_SANDBOX=required` | Require Linux bwrap and fail closed if unavailable or blocked by host policy. |
| `DEVAGENT_SANDBOX=off` | Disable the OS wrapper; structured command and secret/environment controls still apply. |
| `DEVAGENT_NETWORK=deny` | Default; isolate bwrap-managed commands from the host network. |
| `DEVAGENT_NETWORK=inherit` | Explicitly inherit host networking for approved operations. |

For production or customer repositories where host isolation is a hard requirement, use `DEVAGENT_SANDBOX=required`.
