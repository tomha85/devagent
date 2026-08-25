# Model-neutral role routing

DevAgent keeps model choice separate from the deterministic engineering harness. A single default provider still works exactly as before, while optional role overrides let one run use different providers/models for investigation, planning, implementation, and independent review.

## Roles

- `investigator` handles repository understanding requests.
- `planner` handles plan and replan requests.
- `implementer` handles implementation, diagnosis, and bounded repair requests.
- `reviewer` handles independent review requests.

Unconfigured roles fall back to the default provider/model.

## Configure the default

```bash
devagent setup --provider openai --model gpt-5
```

## Configure individual roles

```bash
devagent setup --role investigator --provider anthropic --model claude-sonnet-4-20250514
devagent setup --role planner --provider xai --model grok-4
devagent setup --role implementer --provider openai --model gpt-5
devagent setup --role reviewer --provider anthropic --model claude-sonnet-4-20250514
```

API keys are never written to the config file. Each route stores only the environment-variable name that should contain its credential.

Show the effective routing table with:

```bash
devagent models
```

Example:

```text
DEVAGENT MODEL ROUTING
default: openai/gpt-5
investigator: anthropic/claude-sonnet-4-20250514
planner: xai/grok-4
implementer: openai/gpt-5
reviewer: anthropic/claude-sonnet-4-20250514
```

## Config file

The same routing can be expressed in `~/.config/devagent/config.toml`:

```toml
[provider]
name = "openai"
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 120

[roles.investigator]
name = "anthropic"
model = "claude-sonnet-4-20250514"
api_key_env = "ANTHROPIC_API_KEY"
timeout_seconds = 120

[roles.reviewer]
name = "xai"
model = "grok-4"
api_key_env = "XAI_API_KEY"
timeout_seconds = 120
```

Role-specific environment overrides use the same names with a role prefix, for example:

```bash
export DEVAGENT_REVIEWER_PROVIDER=anthropic
export DEVAGENT_REVIEWER_MODEL=claude-sonnet-4-20250514
export DEVAGENT_REVIEWER_API_KEY_ENV=ANTHROPIC_API_KEY
```

A one-run CLI override such as `--provider` or `--model` intentionally selects one model for the whole run. This preserves the existing explicit override behavior and makes role routing opt-in.

The routing layer never decides whether work is `VERIFIED`. Models only perform bounded reasoning roles; DevAgent's deterministic harness still owns workspace safety, command execution, verification evidence, review gates, final status, and source-control publication.
