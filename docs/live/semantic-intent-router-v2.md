# DevAgent Live Semantic Intent Router V2

DevAgent Live can use the configured AI provider as a bounded natural-language interpretation layer while keeping PLC diagnosis deterministic and evidence-driven.

## Principle

**LLM understands; DevAgent proves.**

The model does not decide whether a machine is healthy, invent PLC causes, or replace the PLC engineering model. It translates free-form engineer wording into a strict structured intent and, when needed, one exact target already present in the loaded engineering context.

The deterministic Live engine then uses canonical PLC logic and trusted OPC UA evidence to produce the actual diagnosis.

## Start Live with semantic routing

```bash
devagent live assist \
  --project-folder /path/to/customer-project \
  --primary-project plc/Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --ai
```

Use the normal DevAgent provider configuration, or override it for the session:

```bash
devagent live assist \
  --project-folder /path/to/customer-project \
  --primary-project plc/Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --ai \
  --provider openai \
  --model <configured-model>
```

The same provider abstraction supports the existing OpenAI, Anthropic/Claude, Gemini/Google, Grok/xAI, and OpenAI-compatible provider paths.

## Natural onsite questions

With `--ai`, wording does not need to match a fixed phrase table. Examples include:

```text
is system good?
how are we looking?
anything I should worry about?
everything normal on this line?
why won't the conveyor run?
what's up with the drive fault bit?
it quit about 90 seconds back, what caused that?
why?
```

The semantic router returns only a bounded contract such as:

```json
{
  "intent": "SYSTEM_HEALTH",
  "target": null,
  "time_scope": "CURRENT",
  "confidence": 0.98,
  "reason": "The engineer is asking for whole-system current health."
}
```

or:

```json
{
  "intent": "ROOT_CAUSE",
  "target": "RunCmd",
  "time_scope": "CURRENT",
  "confidence": 0.96,
  "reason": "The engineer is asking why the conveyor run output is blocked."
}
```

The JSON route is not a PLC diagnosis. The deterministic Live engine still performs the diagnosis.

## Supported semantic intents

- `SYSTEM_HEALTH`
- `SYSTEM_OVERVIEW`
- `ROOT_CAUSE`
- `TAG_STATUS`
- `HISTORICAL_ROOT_CAUSE`
- `FOLLOW_UP`
- `UNKNOWN`

## Engineering grounding

The router may receive bounded static engineering metadata to interpret human language:

- known output names
- known tag names
- tag descriptions from the canonical engineering export when available
- tag data type and scope
- controller/vendor identity
- the previous validated target for conversational follow-up

It does **not** receive OPC UA runtime values, evidence IDs, or deterministic diagnosis results during intent routing.

Any model-proposed target must resolve exactly to a tag/output already present in the loaded engineering model. Invented or ambiguous targets fail closed.

## Fail-closed behavior

Semantic routing falls back to the existing deterministic question resolver when:

- AI is disabled
- the provider is unavailable
- provider output is malformed
- confidence is below the bounded threshold
- the model returns `UNKNOWN`
- a required target is missing
- a target is invented or ambiguous
- historical/current time scope is inconsistent

## Safety boundary

The deterministic PLC control/write guard runs before semantic routing.

Semantic routing adds no capability to:

- write or force PLC tags
- bypass safety or interlocks
- reset devices
- download PLC logic
- change PLC/controller mode
- start or stop equipment
- invoke PLC control methods

DevAgent Live remains read only.
