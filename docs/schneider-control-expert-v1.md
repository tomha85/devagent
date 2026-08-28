# Schneider EcoStruxure Control Expert — DevAgent V1

DevAgent Schneider V1 performs offline engineering review of exported EcoStruxure Control Expert / Unity Pro PLC engineering information. It does not open the proprietary engineering application, connect to a PLC, or execute Control Expert Simulator.

## Preferred input

Export the project from Control Expert as `.XEF` when possible. DevAgent also accepts granular XML exchange exports:

- `.XSY` — variables
- `.XST` — Structured Text section
- `.XLD` — Ladder section
- `.XBD` — Function Block Diagram section
- `.XSF` — Sequential Function Chart section
- `.XIL` — Instruction List section
- `.XDD` — derived data type
- `.XDB` — DFB export
- `.XHW` — I/O configuration
- `.XCM` — communication/network export

`.STU` and `.STA` are work/archive formats and are not parsed directly. Export `.XEF` first. `.ZEF` is treated as an export-package boundary in V1; extract/export the contained `.XEF` and analyze the XML source rather than relying on archive internals.

## Run

```bash
devagent plc ./Line1.XEF \
  --requirements ./requirements.md \
  --verbose \
  --output-dir ./review/line1
```

A directory of granular exports is also supported:

```bash
devagent plc ./ControlExpertExport/ \
  --requirements ./requirements.md \
  --output-dir ./review/line1
```

## V1 deterministic proof boundary

Eligible for bounded local deterministic proof:

- top-level IEC 61131-3 ST Boolean assignments using identifiers and `AND` / `OR` / `NOT`;
- simple series LD networks composed of normal-open / normal-closed contacts driving one normal coil.

Fail-closed / runtime-required in V1:

- ST `IF` / `CASE` / loops / calls and stateful timer-counter-DFB-EFB behavior;
- complex or branched LD, edge contacts/coils, set/reset/stateful networks, function blocks, jumps/returns;
- FBD, SFC, and IL executable behavior.

Those regions remain `PARTIAL` or `OPAQUE`. DevAgent can preserve source traceability and generate engineer FAT procedures, but it does not promote them to static PASS.

## FAT ownership

DevAgent generates and groups FAT procedures. The PLC engineer executes them using the approved Control Expert Simulator, HIL/test bench, or real Modicon PLC procedure and records PASS/FAIL evidence. Generated tests remain `NOT_RUN` until authenticated execution evidence is imported.

## Current commercial boundary

V1 is a qualified parser/verification foundation, not a claim of complete Schneider language/runtime coverage. Commercial closeout still requires a license-safe real Control Expert export corpus and external simulator/HIL/real-controller execution evidence.
