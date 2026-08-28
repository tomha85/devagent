# Schneider Control Expert production qualification gate

Schneider Control Expert V1-V9 provides the deterministic implementation stack. The production corpus gate is a separate evidence layer: it qualifies how that stack behaves on representative **real Control Expert exports** without changing the theorem boundary or pretending that synthetic fixtures are customer evidence.

## Target

The corpus gate can establish `9/10_STATIC_PRODUCTION_QUALIFIED` when every required real-export family is registered, hash-pinned, engineer-attested, analyzed deterministically twice, and passes the V9 source-support/audit contract.

It does **not** establish 10/10 runtime production proof. Control Expert Simulator, HIL, real Modicon PLC execution, field wiring, I/O timing, process physics, SIL, and PL remain separate runtime/physical evidence gates.

## Required real-export families

A production corpus must cover all of these families with cases whose declarations are supported by the analyzed export:

- `M340`
- `M580`
- `UNITY_LEGACY`
- `MIXED_ST_LD_FBD`
- `DFB_DDT`
- `STATE_MACHINE`
- `INTERLOCK_FAULT_RECOVERY`
- `LARGE_INDUSTRIAL`

The M340, M580, and legacy Unity coverage must come from distinct real export bundle identities. One export may cover several behavioral families when the analyzer can substantiate them, but it cannot be labeled as multiple hardware families.

## Keep proprietary projects out of the repository

Store customer/lab exports in a controlled local or enterprise evidence location. Do not commit proprietary PLC projects to the DevAgent source repository merely to run qualification. The manifest uses relative paths beneath a selected corpus root and refuses paths that escape that root.

Example layout:

```text
/secure/schneider-corpus/
├── corpus.json
├── m340-small/
│   └── project.xef
├── m580-warehouse/
│   └── project.xef
└── unity-legacy/
    └── project.xef
```

## Manifest

Schema: `devagent-schneider-production-corpus-v1`

```json
{
  "schema": "devagent-schneider-production-corpus-v1",
  "corpus_id": "control-expert-production-2026q3",
  "cases": [
    {
      "id": "m580-warehouse-01",
      "path": "m580-warehouse/project.xef",
      "source_kind": "REAL_CONTROL_EXPERT_EXPORT",
      "origin": "LAB",
      "controller_family": "M580",
      "families": [
        "M580",
        "MIXED_ST_LD_FBD",
        "DFB_DDT",
        "STATE_MACHINE",
        "INTERLOCK_FAULT_RECOVERY",
        "LARGE_INDUSTRIAL"
      ],
      "bundle_sha256": "<exact DevAgent source-bundle SHA-256>",
      "attested_by": "qualified-engineer@example.com",
      "exported_at": "2026-08-28T12:00:00-04:00"
    }
  ]
}
```

For a real export, `bundle_sha256`, `attested_by`, and timezone-aware `exported_at` are mandatory. The exact analyzed bundle must match the pinned hash. `SYNTHETIC` cases may be used to exercise the gate, but they never satisfy real-export coverage.

## Evidence substantiation

The manifest cannot self-certify behavioral coverage. DevAgent cross-checks family labels against V1-V9 facts:

- `MIXED_ST_LD_FBD` requires observed ST, LD, and FBD source support regions.
- `DFB_DDT` requires DDT plus DFB type/instance identity.
- `STATE_MACHINE` requires a discovered V5 state machine with transitions.
- `INTERLOCK_FAULT_RECOVERY` requires V6 interlock/permissive evidence and V7 fault/recovery evidence.
- `LARGE_INDUSTRIAL` requires at least 256 KiB of supported export source or at least 200 support regions.
- hardware family identity is an explicit engineer attestation and is checked for consistency with the case declaration.

Every case must also keep V9 accounting complete and export audit clean: no missing normalized statement accounting, inconsistent Control Expert metadata, duplicate section identity, unknown executable source surface, or missing executable source section.

## Run

```bash
python scripts/qualify_schneider_production_corpus.py \
  --manifest /secure/schneider-corpus/corpus.json \
  --corpus-root /secure/schneider-corpus \
  --report .devagent/schneider-production-corpus-qualification.json \
  --markdown .devagent/schneider-production-corpus-qualification.md
```

By default each case is analyzed twice and the deterministic V9 snapshot must match. `--single-pass` exists only for debugging and must not be used as commercial qualification evidence.

The command returns success only for `STATIC_CORPUS_QUALIFIED`. During corpus collection, `--allow-pending` may be used to generate reports while required real families are still missing.

## Status meanings

- `FAIL`: one or more registered cases, hashes, attestations, source audits, or family substantiation checks failed.
- `PENDING_REAL_EXPORT_CORPUS`: registered cases are clean, but required real-export families are still missing.
- `STATIC_CORPUS_QUALIFIED`: every required real-export family is represented by passing real evidence and the deterministic static corpus gate is complete.

`STATIC_CORPUS_QUALIFIED` is the Schneider **9/10 static production-readiness evidence point**. Runtime/physical 10/10 remains an explicit separate gate; DevAgent must never silently promote static corpus evidence into Simulator/HIL/real-PLC proof.
