# DevAgent — General Architecture

DevAgent has one core, but **software engineering and PLC engineering are two different workflows**. They share evidence, review, and reporting principles, but they do not perform the same kind of work.

```text
                              DevAgent Core
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
          Software Engineering             PLC Engineering
                    │                             │
          Local / GitHub Repo          ┌──────────┼──────────┐
                    │                  │          │          │
          Understand / Plan         Siemens    Rockwell   Schneider
                    │               TIA        Studio     Control
             Modify Code            exports    5000/L5X   Expert/XEF
                    │                  │          │          │
           Build / Test / Review       └──────────┼──────────┘
                    │                             │
          Engineering Report              Analyze / Verify
                    │                             │
          Commit / Push Branch             FAT Plan / Report
                    │
       Developer / Repo Integration
```

### Software Engineering

DevAgent works directly with a software working repository. It can understand the repository, implement a bounded code change, run repository-native tests/builds, independently review the result, produce an engineering report, and publish a verified commit to a safe branch. Pull-request approval and merge remain part of the developer/repository integration workflow rather than the PLC workflow.

### PLC Engineering

DevAgent works from exported PLC engineering artifacts rather than editing or controlling the live PLC. Siemens, Rockwell, and Schneider each have a vendor-specific import path, but all feed the PLC analysis workflow for logic review, requirement verification, risk detection, FAT planning, regression analysis, evidence, and engineering reporting.

In simple terms:

```text
Software:  Working Repo → Change → Test → Review → Report → Commit/Push
PLC:       PLC Export   → Analyze → Verify → FAT Plan → Engineering Report
```

The two branches share the DevAgent evidence-driven core, but their execution and release workflows intentionally remain separate.
