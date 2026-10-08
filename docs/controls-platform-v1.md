# DevAgent Controls Platform V1

DevAgent Controls V1 introduces a deterministic authoring boundary above the
existing DevAgent PLC verification product.

## Product boundary

The V1 pipeline is intentionally limited to:

```text
ControlSystemSpec
  -> strict fail-closed validation
  -> deterministic normalization
  -> ControlsIR
  -> stable SHA-256 identities
```

V1 does **not** generate PLC code, generate Ignition projects, invoke Studio
5000/TIA Portal/Control Expert, connect to a controller, change controller
mode, write/force/reset tags, or alter DevAgent Live.

The existing products remain authoritative for their own domains:

- DevAgent PLC: imported engineering artifacts, deterministic analysis,
  requirement verification, FAT planning/evidence, and release readiness.
- DevAgent Live: read-only onsite commissioning evidence and diagnosis.
- DevAgent Controls: authoring intent only.

## Why a separate authoring IR

`CanonicalPLCProject` describes an imported PLC artifact and includes source
provenance and semantic-coverage facts. Those are verification concerns, not
authoring intent.

`ControlsIR` therefore starts from a separate `ControlSystemSpec`. Future
vendor generators will compile from this IR, then their output will be
re-imported by DevAgent PLC and compared against the expected projection. This
creates a round-trip verification boundary rather than trusting a generator's
own output.

## V1 schema

The schema is `devagent-controls-spec-v1`. It supports project/controller
identity, MOTOR/VFD/CONVEYOR/VALVE equipment, versioned standards, declared
signals, commands, status, permissives, interlocks, alarms, HMI metadata, and
requirement metadata.

Unknown fields and unknown schema versions fail closed. Engineering-significant
fields are explicit; the parser avoids behavior-changing implicit defaults.

V1 accepts only catalog-qualified standards: `motor-v1`, `vfd-v1`,
`conveyor-v1`, and `valve-v1`. A version-shaped but unqualified name such as
`conveyor-v999` fails closed. Engineering identities are also checked for
case-insensitive collisions so cross-vendor naming does not become ambiguous.

## Determinism contract

Semantically unordered collections are normalized before hashing. Reordering
controllers, equipment, signals, commands, alarms, or requirements must not
change the normalized bytes or hashes.

The authoring surface exposes:

- `spec_sha256`: canonical validated specification identity;
- `controls_ir_sha256`: canonical Controls IR identity.

A semantic engineering change must change the Controls IR hash. The in-memory
Controls IR is deeply immutable: it contains frozen dataclasses and tuples,
not mutable dictionaries or lists. This prevents post-hash authoring drift.

## Future phases

```text
ControlsIR
  -> deterministic Rockwell generator
  -> deterministic Ignition generator
  -> candidate build
  -> DevAgent PLC re-import
  -> round-trip comparison
  -> company standards analyzer
  -> FAT/simulation
  -> qualified evidence
  -> human engineering approval
```

Production deployment remains a separate controlled authority and is not part
of the V1 authoring branch.
