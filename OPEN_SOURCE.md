# DevAgent Open-Source Boundary

DevAgent is intended to maintain a transparent, local-first open-source core for evidence-driven software engineering, PLC engineering review, and read-only commissioning assistance.

## License of this repository

Unless a file states otherwise, source code and documentation committed to this repository are licensed under the MIT License in [LICENSE](LICENSE).

A version that has already been released under the MIT License remains available under those MIT terms. Future commercial products, services, private datasets, or separately distributed components are not automatically covered by this repository's MIT License unless they are explicitly released here under that license.

## What belongs in the public core

The public core may include:

- DevAgent software-engineering orchestration and evidence contracts;
- deterministic safety, verification, review, reporting, and bounded publication logic;
- provider-neutral model routing and local execution support;
- DevAgent PLC parsers, canonical engineering models, public semantic analysis, public FAT/report contracts, and license-safe deterministic fixtures;
- DevAgent Live read-only OPC UA runtime, reconciliation, trust/freshness/history logic, diagnosis contracts, and public deterministic fixtures;
- public qualification harnesses, synthetic examples, documentation, and regression tests that can be safely redistributed.

## What must not be committed to the public core

Do not commit private or commercially sensitive assets such as:

- customer PLC projects, exports, requirements, reports, or site configurations;
- customer OPC UA namespaces, endpoint credentials, certificates, mappings, or runtime captures;
- customer evidence history, incident history, or proprietary operational data;
- confidential field-failure corpora or production compatibility intelligence;
- privately licensed vendor artifacts that are not redistributable;
- private qualification corpora, proprietary certification packs, or customer-specific semantic/rule packs;
- enterprise credentials, deployment secrets, support records, or private integration configuration;
- any material whose license, NDA, contract, or customer agreement does not permit public redistribution.

Use synthetic, independently created, public-domain, permissively licensed, or otherwise redistribution-safe fixtures in this repository.

## Public qualification vs. commercial qualification

Public qualification should prove the behavior and truthfulness of the open-source core using reproducible, redistribution-safe cases.

Commercial or private qualification may additionally use real vendor environments, private customer projects, field observations, compatibility matrices, longer-running evidence, and customer-specific workflows. Those assets should live outside this public repository unless their owners explicitly authorize publication and redistribution.

A public test result is evidence for the exact published case and environment. It is not a universal certification of every unseen PLC project, vendor version, network, model, site, or customer workflow.

## Commercial products and services

The existence of an MIT open-source core does not prevent separate commercial offerings. Future commercial offerings may include, for example:

- certified qualification or compatibility packs;
- enterprise evidence registries and audit workflows;
- private/on-premises deployment and administration;
- customer-specific integrations and engineering packs;
- managed support, qualification assistance, or service-level agreements;
- private field-compatibility intelligence and customer-specific evidence systems.

Such offerings are separate from this repository unless explicitly committed here under the MIT License.

## Project identity

The MIT License covers the code and documentation distributed under it. It does not itself grant trademark rights in the DevAgent project name or logos. See [TRADEMARKS.md](TRADEMARKS.md).

## Contributions

By contributing code or documentation to this repository, contributors agree that accepted contributions are licensed under the repository's MIT License. See [CONTRIBUTING.md](CONTRIBUTING.md).

Do not contribute customer, employer, vendor, or third-party confidential material unless you have explicit authority to redistribute it under the repository license.
