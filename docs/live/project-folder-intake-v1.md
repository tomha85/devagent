# DevAgent Live Project Folder Intake V1

DevAgent Live can start from a customer engineering workspace instead of requiring the engineer to point at one PLC file manually.

## Example workspace

```text
customer-line1/
├── plc/
│   └── Line1.L5X
├── io/
│   └── IO_List.csv
├── tags/
│   └── Tag_Descriptions.xlsx
├── requirements/
│   └── FDS.md
├── fat/
│   └── FAT_tests.csv
└── drawings/
    └── conveyor_layout.pdf
```

Start Live with the folder:

```bash
ENDPOINT="opc.tcp://192.168.10.20:4840/"

devagent live assist \
  --project-folder /path/to/customer-line1 \
  --endpoint "$ENDPOINT"
```

When exactly one supported authoritative PLC engineering source is found, Live selects it automatically. Supported authoritative inputs remain the existing vendor surfaces: Rockwell Studio 5000 `.L5X`, Siemens TIA exported source/XML bundles, and Schneider Control Expert `.XEF`/X* exchange exports.

## Ambiguous workspaces

Live fails closed when a folder contains more than one plausible authoritative engineering project. It does not guess which PLC project should control diagnosis.

Select the intended source explicitly:

```bash
devagent live assist \
  --project-folder /path/to/customer-line1 \
  --primary-project plc/Line1.L5X \
  --endpoint "$ENDPOINT"
```

`--primary-project` must resolve inside the selected project folder.

## Supplemental engineering context

The workspace scanner inventories files such as:

- I/O lists
- tag-description tables
- requirements/specifications
- FAT/SAT/commissioning test artifacts
- drawings/schematics
- CSV/TSV/JSON/YAML/Markdown/text/Office/PDF support files

Use the interactive command:

```text
:workspace
```

to inspect the selected authoritative PLC input, detected vendor, file classifications, and authority boundary.

## Authority boundary

Project-folder convenience does not weaken DevAgent Live's evidence rules.

1. Canonical PLC logic comes only from the authoritative supported PLC engineering export.
2. I/O lists, tag descriptions, requirements, FAT files, drawings, and other documents are supplemental context; they must not override PLC logic semantics.
3. Runtime conclusions still require safely reconciled trusted CURRENT OPC UA evidence.
4. Ambiguous engineering sources fail closed until the engineer selects the intended primary project.
5. DevAgent Live remains read only; folder intake adds no PLC write, force, bypass, mode, download, start/stop, or method-control capability.

## Direct-file compatibility

The existing direct-file workflow remains supported:

```bash
devagent live assist \
  examples/live/warehouse_commissioning_demo.L5X \
  --endpoint "$ENDPOINT"
```

Use `--project-folder` when the customer hands over a complete engineering package and you want Live to preserve an auditable inventory around the authoritative PLC project.
