# DevAgent — General Architecture

This is the high-level view of DevAgent. The same DevAgent core supports both software engineering and PLC engineering through vendor-specific inputs.

```text
                         DevAgent Core
                              │
        ┌─────────────┬───────┴───────┬─────────────┐
        │             │               │             │
     Software      Siemens         Rockwell      Schneider
        │             │               │             │
 Python / etc.    TIA Portal      Studio 5000    Control Expert
                  exports          / L5X          / XEF
        │             │               │             │
        └─────────────┴───────┬───────┴─────────────┘
                              │
                    Review → Verify → FAT → Report
```

In simple terms: **one DevAgent core, multiple engineering inputs, one evidence-driven review and reporting workflow.**

- **Software** — works with normal software repositories such as Python, JavaScript/TypeScript, Java, .NET, C/C++, Go, Rust, and others supported by repository-native tooling.
- **Siemens** — analyzes supported TIA Portal exported engineering artifacts.
- **Rockwell** — analyzes Studio 5000 / Logix Designer `.L5X` exports.
- **Schneider** — analyzes EcoStruxure Control Expert / Unity Pro exports with `.XEF` preferred.

DevAgent keeps deterministic evidence, engineering review, FAT planning, and reporting separate from simulator, HIL, or real-controller execution.
