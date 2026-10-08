# DevAgent Controls Platform V1

DevAgent Controls adds a deterministic controls-authoring and staging layer
above the existing DevAgent PLC verification engine.

## Authority boundary

The Controls product can:

- validate a company controls specification;
- normalize it into an immutable Controls IR;
- enforce versioned Motor/VFD/Conveyor/Valve standards;
- generate deterministic Rockwell full-project `.L5X` staging artifacts;
- re-import those artifacts through the existing DevAgent Rockwell analyzer;
- generate Ignition staging configuration from the same equipment identity;
- generate FAT plans from standard intent;
- run a deterministic standard-model simulation;
- package evidence and hashes;
- expose the same workflows through CLI and a local self-service portal.

It does **not** connect to a PLC, force/write a tag, change controller mode,
download a PLC project, deploy an Ignition Gateway project, or claim external
FAT execution.

The release-readiness output is therefore
`READY_FOR_ENGINEERING_REVIEW`, never production release approval.

## Architecture

```text
ControlSystemSpec
  -> strict fail-closed validation
  -> immutable ControlsIR
  -> company standards
       |
       +-> Rockwell generator -> .L5X
       |                         |
       |                         +-> existing DevAgent PLC analyzer
       |                              -> round-trip proof
       |
       +-> Ignition staging generator
       |      -> UDT-equivalent definitions
       |      -> equipment instances
       |      -> alarm bindings
       |      -> history policy
       |      -> faceplate bindings
       |
       +-> FAT generator
              -> deterministic standard-model simulation

all outputs
  -> SHA-256 generation manifest
  -> self-verification
  -> engineering review
```

## V1 company standards

V1 explicitly qualifies four standard IDs:

- `motor-v1`
- `vfd-v1`
- `conveyor-v1`
- `valve-v1`

A version-shaped but unqualified name such as `conveyor-v999` fails closed.
Required command/status contracts are checked by the company rules engine.

HIGH and CRITICAL alarms require an explicit `source_signal`; lower-priority
alarms without a source are retained as warnings. This prevents a generated
HMI from pretending that an alarm is bound when its PLC source is unknown.

## Determinism

The following identities are stable for the same semantic input:

- `spec_sha256`
- `controls_ir_sha256`
- generated Rockwell bytes;
- Ignition staging JSON;
- FAT plan;
- build artifact hashes.

Reordering semantically unordered input does not change the normalized IR.
Engineering-significant changes do change the IR hash.

Rockwell identifiers are deterministically normalized to Logix-safe names and
bounded to 40 characters. Long identifiers receive a stable hash suffix.

## Rockwell generation

V1 generation supports Rockwell `CONTROLLOGIX` and `COMPACTLOGIX`
controllers. The generator emits simple bounded RLL using only the already
supported deterministic instruction surface (`XIC`, `XIO`, `OTE`).

Generated projects are not trusted merely because DevAgent created them.
Every generated project is re-imported through
`devagent.plc.safe_analysis.analyze_rockwell_l5x`.

Round-trip verification checks:

- controller identity;
- exact generated tag inventory;
- exact generated rung sequence/text;
- no unknown instructions;
- exactly one writer for every generated output;
- existing DevAgent PLC outcome is `STATICALLY_VERIFIED`.

A mismatch blocks build finalization.

## Ignition staging

Ignition artifacts deliberately use the schema
`devagent-ignition-staging-v1`. They are deterministic staging data, not a
claim that DevAgent has changed a running Gateway.

PLC, HMI, historian, alarm, FAT, and requirement traceability all retain the
same stable equipment ID, for example `CONV_101`.

## FAT and simulation boundary

Controls V1 generates deterministic standard FAT cases for positive paths,
permissive loss, interlock activation, stop/opposite-command inhibition, and
reset where applicable.

The generated plan always says:

```text
execution_status = NOT_RUN
execution_owner  = CONTROLS_ENGINEER
```

The built-in simulation is a deterministic **standard model** check only.
It proves the generated intent is internally self-consistent. It is not
FactoryTalk Logix Echo, HIL, a real PLC, process physics, or machine safety
validation.

Qualified runtime evidence and human engineering approval remain downstream
requirements before production release.

## CLI

Validate:

```bash
devagent controls validate examples/controls/conveyor_v1.json
```

Inspect normalized intent:

```bash
devagent controls inspect examples/controls/conveyor_v1.json
```

Build the complete staging package:

```bash
devagent controls build \
  examples/controls/conveyor_v1.json \
  --output-dir /tmp/controls-build
```

`generate` is an alias of `build`.

Verify an existing build:

```bash
devagent controls verify /tmp/controls-build
```

Run the local self-service portal:

```bash
devagent controls portal \
  --workspace .devagent/controls-portal \
  --host 127.0.0.1 \
  --port 8765
```

Remote portal binding is refused unless the operator explicitly supplies
`--allow-remote`.

## Build package

A successful build contains:

```text
build/
  input/spec.json
  controls-ir.json
  company-standards.json
  generation-manifest.json
  release-readiness.json

  rockwell/
    <controller>.L5X
    symbol-map.json

  ignition/
    udts.json
    equipment.json
    alarms.json
    history.json
    views.json

  tests/
    fat-plan.json
    model-simulation.json

  verification/
    rockwell-roundtrip.json
    ignition-binding.json
```

Every file except the manifest itself is SHA-256 bound by
`generation-manifest.json`. `devagent controls verify` fails if files are
missing, added, changed, non-deterministic, or no longer round-trip.

## CI/CD boundary

`.github/workflows/controls-qualification.yml` performs the first vertical
slice in CI:

```text
validate
 -> focused controls tests
 -> build
 -> round-trip verification
 -> Ignition coherence
 -> FAT generation
 -> deterministic model simulation
 -> reproducibility proof
 -> evidence artifact upload
```

This is CI and staging artifact delivery. Production PLC/HMI deployment is
intentionally a separate controlled authority.

## First qualified vertical slice

The included example is:

```text
CONV_101
  + Rockwell ControlLogix
  + conveyor-v1
  + explicit high-priority drive-fault alarm binding
  + standard HMI faceplate
  + historian
  + deterministic FAT
```

The branch acceptance target is:

```text
SPEC_VALIDATION=PASS
COMPANY_STANDARD_RULES=PASS
DETERMINISTIC_GENERATION=PASS
ROCKWELL_REIMPORT=PASS
CONTROLS_IR_ROUNDTRIP=PASS
IGNITION_BINDING_COHERENCE=PASS
FAT_GENERATION=PASS
MODEL_SIMULATION=PASS
UNQUALIFIED_DYNAMIC_PASS=0
HUMAN_APPROVAL_REQUIRED=PASS
PRODUCTION_DEPLOYMENT=NOT_PERFORMED
```
