from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from devagent.plc.production_models import RequirementStatus
from devagent.plc.production_v5 import run_production_verification_v5

BENCHMARK_SCHEMA = "devagent-rockwell-warehouse-benchmark-v1"
BENCHMARK_NAME = "Warehouse_Sortation_Benchmark"

PROGRAMS = (
    "SystemControl",
    "Infeed",
    "MergeArea",
    "MainSortation",
    "Sorter",
    "Chutes",
    "Diagnostics",
)

DEFECTS: tuple[dict[str, Any], ...] = (
    {"id": "D01", "category": "DOWNSTREAM_INTERLOCK", "severity": "HIGH", "subject": "CV017", "description": "CV017 can start while downstream CV018 is not ready.", "requirement_id": "REQ-D01"},
    {"id": "D02", "category": "TIMING", "severity": "MEDIUM", "subject": "CV010_JamTimer", "description": "CV010 jam timer preset is 200 ms instead of 2000 ms.", "requirement_id": "REQ-D02"},
    {"id": "D03", "category": "FAULT_INJECTION_TEST", "severity": "MEDIUM", "subject": "CV005_PE_In", "description": "Photoeye stuck-ON negative test must be generated/executed.", "requirement_id": "REQ-D03"},
    {"id": "D04", "category": "MISSING_PERMISSIVE", "severity": "HIGH", "subject": "CV021_VFDReady", "description": "CV021 run command ignores VFD ready.", "requirement_id": "REQ-D04"},
    {"id": "D05", "category": "MISSING_INTERLOCK", "severity": "HIGH", "subject": "CV022_VFDFault", "description": "CV022 run command ignores VFD fault.", "requirement_id": "REQ-D05"},
    {"id": "D06", "category": "MERGE_ARBITRATION", "severity": "HIGH", "subject": "Merge01", "description": "Two merge conveyors can be released simultaneously.", "requirement_id": "REQ-D06"},
    {"id": "D07", "category": "SORT_ROUTING", "severity": "HIGH", "subject": "DIV03_Fire", "description": "Destination 3 is compared against the wrong sorter destination.", "requirement_id": "REQ-D07"},
    {"id": "D08", "category": "TRACKING", "severity": "HIGH", "subject": "TrackingIndex", "description": "Package tracking advances by two slots instead of one.", "requirement_id": "REQ-D08"},
    {"id": "D09", "category": "CAPACITY_INTERLOCK", "severity": "HIGH", "subject": "CH07_Full", "description": "DIV07 ignores chute-full status.", "requirement_id": "REQ-D09"},
    {"id": "D10", "category": "SAFETY_RESET", "severity": "CRITICAL", "subject": "MasterFault", "description": "Fault reset can clear while E-stop health is false.", "requirement_id": "REQ-D10"},
    {"id": "D11", "category": "TIMER_RESET", "severity": "MEDIUM", "subject": "CV011_JamTimer", "description": "CV011 jam timer has no reset path.", "requirement_id": "REQ-D11"},
    {"id": "D12", "category": "UNREACHABLE_LOGIC", "severity": "LOW", "subject": "DeadRoutine", "description": "An unreachable diagnostic routine is exported but never scheduled/called.", "requirement_id": None},
    {"id": "D13", "category": "UNUSED_TAG", "severity": "LOW", "subject": "Unused_Engineering_Tag", "description": "Unused controller tag remains in the project.", "requirement_id": None},
    {"id": "D14", "category": "DUPLICATE_LOGIC", "severity": "LOW", "subject": "CV025_RunCmd", "description": "CV025 contains duplicate command logic.", "requirement_id": None},
    {"id": "D15", "category": "MULTIPLE_WRITERS", "severity": "HIGH", "subject": "CV030_RunCmd", "description": "Maintenance override creates an uncontrolled second writer for CV030_RunCmd.", "requirement_id": None},
    {"id": "D16", "category": "LOST_PACKAGE", "severity": "HIGH", "subject": "LostPackageTimer", "description": "Lost-package timeout logic is missing.", "requirement_id": "REQ-D16"},
    {"id": "D17", "category": "MANUAL_MODE_INTERLOCK", "severity": "CRITICAL", "subject": "CV027_RunCmd", "description": "Manual mode bypasses E-stop permissive on CV027.", "requirement_id": "REQ-D17"},
    {"id": "D18", "category": "FAULT_RECOVERY", "severity": "HIGH", "subject": "CV005_FaultLatched", "description": "Fault latch can reset while the VFD fault remains active.", "requirement_id": "REQ-D18"},
    {"id": "D19", "category": "BARCODE_NO_READ", "severity": "HIGH", "subject": "PackageDestination", "description": "Barcode no-read is routed to chute 1 instead of reject destination 0.", "requirement_id": "REQ-D19"},
    {"id": "D20", "category": "TRACKING_PERMISSIVE", "severity": "HIGH", "subject": "DIV08_Fire", "description": "DIV08 can fire without a tracked package present.", "requirement_id": "REQ-D20"},
)


def _tag(name: str, data_type: str = "BOOL") -> str:
    return f'<Tag Name="{name}" TagType="Base" DataType="{data_type}" />'


def _rung(number: int, text: str, comment: str | None = None) -> str:
    description = f"<Comment><![CDATA[{comment}]]></Comment>" if comment else ""
    return f'<Rung Number="{number}" Type="N">{description}<Text><![CDATA[{text}]]></Text></Rung>'


def _rll_routine(name: str, rungs: list[str]) -> str:
    return f'<Routine Name="{name}" Type="RLL"><RLLContent>{"".join(rungs)}</RLLContent></Routine>'


def _st_routine(name: str, lines: list[str]) -> str:
    encoded = "".join(
        f'<Line Number="{index}"><![CDATA[{line}]]></Line>'
        for index, line in enumerate(lines)
    )
    return f'<Routine Name="{name}" Type="ST"><STContent>{encoded}</STContent></Routine>'


def _main_routine(calls: list[str]) -> str:
    rungs = [_rung(index, f"JSR({name},0);", f"Execute {name}") for index, name in enumerate(calls)]
    return _rll_routine("Main", rungs)


def _conveyor_routine(index: int, *, defective: bool) -> str:
    cv = f"CV{index:03d}"
    downstream = f"CV{index + 1:03d}_Ready" if index < 40 else "SystemDownstreamReady"
    permissives = [
        "XIC(AutoMode)",
        "XIC(EStopHealthy)",
        f"XIC({cv}_Ready)",
        f"XIC({cv}_VFDReady)",
        f"XIO({cv}_VFDFault)",
        f"XIO({cv}_Fault)",
        f"XIO({cv}_Jam)",
        f"XIC({downstream})",
    ]
    if defective and index == 17:
        permissives.remove(f"XIC({downstream})")
    if defective and index == 21:
        permissives.remove(f"XIC({cv}_VFDReady)")
    if defective and index == 22:
        permissives.remove(f"XIO({cv}_VFDFault)")

    rungs = [
        _rung(0, "".join(permissives) + f"OTE({cv}_RunCmd);", "Automatic conveyor permissives"),
        _rung(1, f"XIC({cv}_PE_Out)OTE({cv}_Occupied);", "Package occupancy"),
        _rung(
            2,
            f"XIC({cv}_RunCmd)XIC({cv}_PE_In)TON({cv}_JamTimer,{200 if defective and index == 10 else 2000},0);",
            "Jam detection timer",
        ),
        _rung(3, f"XIC({cv}_JamTimer.DN)OTE({cv}_Jam);", "Jam alarm"),
    ]
    if not (defective and index == 11):
        rungs.append(_rung(4, f"XIO({cv}_PE_In)RES({cv}_JamTimer);", "Reset jam timer when sensor clears"))

    if index == 27:
        manual = ["XIC(ManualMode)", f"XIC({cv}_ManualPB)", "XIC(EStopHealthy)", f"XIO({cv}_Fault)"]
        if defective:
            manual.remove("XIC(EStopHealthy)")
        rungs.append(_rung(5, "".join(manual) + f"OTE({cv}_RunCmd);", "Manual jog path"))
    if defective and index == 25:
        rungs.append(_rung(6, "".join(permissives) + f"OTE({cv}_RunCmd);", "Seeded duplicate command logic"))
    if defective and index == 30:
        rungs.append(_rung(7, f"XIC(MaintenanceOverride)OTE({cv}_RunCmd);", "Seeded uncontrolled second writer"))
    return _rll_routine(cv, rungs)


def _diverter_routine(index: int, *, defective: bool) -> str:
    div = f"DIV{index:02d}"
    chute = f"CH{index:02d}_Full"
    destination = index
    if defective and index == 3:
        destination = 4
    parts = [f"EQU(PackageDestination,{destination})", "XIC(PackagePresent)", f"XIO({chute})"]
    if defective and index == 7:
        parts.remove(f"XIO({chute})")
    if defective and index == 8:
        parts.remove("XIC(PackagePresent)")
    return _rll_routine(div, [_rung(0, "".join(parts) + f"OTE({div}_Fire);", "Destination and chute permissives")])


def _system_control(defective: bool) -> str:
    routines = [
        _main_routine(["AutoMode", "ManualMode", "EStop", "FaultReset"]),
        _rll_routine("AutoMode", [_rung(0, "XIC(AutoModePB)XIC(EStopHealthy)OTE(AutoMode);")]),
        _rll_routine("ManualMode", [_rung(0, "XIC(ManualModePB)XIC(EStopHealthy)OTE(ManualMode);")]),
        _rll_routine("EStop", [_rung(0, "XIO(EStopHealthy)OTL(MasterFault);")]),
        _rll_routine(
            "FaultReset",
            [_rung(0, ("XIC(FaultResetPB)" + ("" if defective else "XIC(EStopHealthy)") + "OTU(MasterFault);"), "Fault reset must respect E-stop health")],
        ),
    ]
    return f'<Program Name="SystemControl" MainRoutineName="Main"><Routines>{"".join(routines)}</Routines></Program>'


def _program_with_conveyors(name: str, start: int, end: int, extras: list[str], *, defective: bool) -> str:
    conveyor_names = [f"CV{index:03d}" for index in range(start, end + 1)]
    calls = conveyor_names + [extra.split('Name="', 1)[1].split('"', 1)[0] for extra in extras]
    routines = [_main_routine(calls)]
    routines.extend(_conveyor_routine(index, defective=defective) for index in range(start, end + 1))
    routines.extend(extras)
    return f'<Program Name="{name}" MainRoutineName="Main"><Routines>{"".join(routines)}</Routines></Program>'


def _merge_routines(defective: bool) -> list[str]:
    cv11 = "XIC(Merge01_CV011_Request)XIC(Merge01_Turn)"
    cv12 = "XIC(Merge01_CV012_Request)XIO(Merge01_Turn)"
    if not defective:
        cv11 += "XIO(Merge01_CV012_Release)"
        cv12 += "XIO(Merge01_CV011_Release)"
    merge1 = _rll_routine(
        "Merge01",
        [
            _rung(0, cv11 + "OTE(Merge01_CV011_Release);", "Mutually exclusive merge release A"),
            _rung(1, cv12 + "OTE(Merge01_CV012_Release);", "Mutually exclusive merge release B"),
        ],
    )
    merge2 = _rll_routine(
        "Merge02",
        [
            _rung(0, "XIC(Merge02_CV019_Request)XIC(Merge02_Turn)XIO(Merge02_CV020_Release)OTE(Merge02_CV019_Release);"),
            _rung(1, "XIC(Merge02_CV020_Request)XIO(Merge02_Turn)XIO(Merge02_CV019_Release)OTE(Merge02_CV020_Release);"),
        ],
    )
    return [merge1, merge2]


def _main_sortation_extras(defective: bool) -> list[str]:
    tracking = _st_routine(
        "Tracking",
        [
            "IF EncoderPulse THEN",
            f"TrackingIndex := TrackingIndex + {2 if defective else 1};",
            "END_IF;",
        ],
    )
    encoder = _rll_routine("Encoder", [_rung(0, "XIC(EncoderPulse)CTU(EncoderCount,1,0);")])
    sort_lines = [
        "IF BarcodeValid THEN",
        "PackageDestination := BarcodeDestination;",
        "ELSE",
        f"PackageDestination := {1 if defective else 0};",
        "END_IF;",
    ]
    return [tracking, encoder, _st_routine("SortDecision", sort_lines)]


def _sorter_extras(defective: bool) -> list[str]:
    return [_diverter_routine(index, defective=defective) for index in range(1, 9)]


def _chutes_program() -> str:
    names = [f"CH{index:02d}" for index in range(1, 17)]
    routines = [_main_routine(names)]
    for index, name in enumerate(names, start=1):
        routines.append(_rll_routine(name, [_rung(0, f"XIC({name}_PE)OTE({name}_Occupied);")]))
    return f'<Program Name="Chutes" MainRoutineName="Main"><Routines>{"".join(routines)}</Routines></Program>'


def _diagnostics_program(defective: bool) -> str:
    calls = ["JamDetection", "TrackingFault", "MotorFault", "PhotoeyeBlocked", "CommunicationFault"]
    routines = [_main_routine(calls)]
    routines.append(_rll_routine("JamDetection", [_rung(0, "XIC(AnyJam)OTE(JamSummary);")]))
    tracking_rungs = [] if defective else [_rung(0, "XIC(PackagePresent)XIO(TrackingHealthy)TON(LostPackageTimer,3000,0);")]
    routines.append(_rll_routine("TrackingFault", tracking_rungs or [_rung(0, "XIC(TrackingFaultPB)OTE(TrackingFault);")]))
    motor_reset = "XIC(FaultResetPB)" + ("" if defective else "XIO(CV005_VFDFault)") + "OTU(CV005_FaultLatched);"
    routines.append(
        _rll_routine(
            "MotorFault",
            [
                _rung(0, "XIC(CV005_VFDFault)OTL(CV005_FaultLatched);"),
                _rung(1, motor_reset),
            ],
        )
    )
    routines.append(_rll_routine("PhotoeyeBlocked", [_rung(0, "XIC(CV005_PE_In)TON(PE005_StuckTimer,5000,0);")]))
    routines.append(_rll_routine("CommunicationFault", [_rung(0, "XIO(BarcodeCommHealthy)OTE(CommunicationFault);")]))
    if defective:
        routines.append(_rll_routine("DeadRoutine", [_rung(0, "XIC(DeadRoutineTrigger)OTE(DeadRoutineOutput);")]))
    return f'<Program Name="Diagnostics" MainRoutineName="Main"><Routines>{"".join(routines)}</Routines></Program>'


def _aoi_definitions() -> str:
    return '''<AddOnInstructionDefinitions>
<AddOnInstructionDefinition Name="ConveyorAOI">
  <Parameters>
    <Parameter Name="EnableIn" Usage="Input" DataType="BOOL" />
    <Parameter Name="EnableOut" Usage="Output" DataType="BOOL" />
    <Parameter Name="Permissive" Usage="Input" DataType="BOOL" Required="true" Visible="true" />
    <Parameter Name="Run" Usage="Output" DataType="BOOL" Required="true" Visible="true" />
  </Parameters>
  <Routines><Routine Name="Logic" Type="RLL"><RLLContent>
    <Rung Number="0" Type="N"><Text><![CDATA[XIC(Permissive)OTE(Run);]]></Text></Rung>
  </RLLContent></Routine></Routines>
</AddOnInstructionDefinition>
</AddOnInstructionDefinitions>'''


def _all_tags(defective: bool) -> list[str]:
    tags = [
        _tag(name)
        for name in (
            "AutoModePB", "ManualModePB", "AutoMode", "ManualMode", "EStopHealthy", "FaultResetPB", "MasterFault",
            "SystemDownstreamReady", "MaintenanceOverride", "PackagePresent", "BarcodeValid", "BarcodeCommHealthy",
            "TrackingHealthy", "TrackingFaultPB", "TrackingFault", "AnyJam", "JamSummary", "CommunicationFault",
            "EncoderPulse", "Merge01_CV011_Request", "Merge01_CV012_Request", "Merge01_Turn", "Merge01_CV011_Release",
            "Merge01_CV012_Release", "Merge02_CV019_Request", "Merge02_CV020_Request", "Merge02_Turn",
            "Merge02_CV019_Release", "Merge02_CV020_Release", "CV005_FaultLatched", "DeadRoutineTrigger", "DeadRoutineOutput",
        )
    ]
    tags.extend([_tag("PackageDestination", "DINT"), _tag("BarcodeDestination", "DINT"), _tag("TrackingIndex", "DINT")])
    tags.extend([_tag("EncoderCount", "COUNTER"), _tag("LostPackageTimer", "TIMER"), _tag("PE005_StuckTimer", "TIMER")])
    for index in range(1, 41):
        cv = f"CV{index:03d}"
        for suffix in ("Ready", "VFDReady", "VFDFault", "Fault", "Jam", "PE_In", "PE_Out", "Occupied", "RunCmd", "ManualPB"):
            tags.append(_tag(f"{cv}_{suffix}"))
        tags.append(_tag(f"{cv}_JamTimer", "TIMER"))
    for index in range(1, 9):
        tags.append(_tag(f"DIV{index:02d}_Fire"))
    for index in range(1, 17):
        tags.extend([_tag(f"CH{index:02d}_Full"), _tag(f"CH{index:02d}_PE"), _tag(f"CH{index:02d}_Occupied")])
    for index in range(1, 5):
        tags.extend([_tag(f"ConveyorAOI_{index}", "ConveyorAOI"), _tag(f"AOI{index}_Permissive"), _tag(f"AOI{index}_Run")])
    if defective:
        tags.append(_tag("Unused_Engineering_Tag", "DINT"))
    return tags


def build_l5x(*, defective: bool) -> str:
    infeed_extra = [_rll_routine("BarcodeTunnel", [_rung(0, "XIC(BarcodeCommHealthy)OTE(BarcodeValid);")])]
    programs = [
        _system_control(defective),
        _program_with_conveyors("Infeed", 1, 10, infeed_extra, defective=defective),
        _program_with_conveyors("MergeArea", 11, 20, _merge_routines(defective), defective=defective),
        _program_with_conveyors("MainSortation", 21, 30, _main_sortation_extras(defective), defective=defective),
        _program_with_conveyors("Sorter", 31, 40, _sorter_extras(defective), defective=defective),
        _chutes_program(),
        _diagnostics_program(defective),
    ]
    scheduled = "".join(f'<ScheduledProgram Name="{name}" />' for name in PROGRAMS)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="{BENCHMARK_NAME}" TargetType="Controller">
<Controller Use="Target" Name="{BENCHMARK_NAME}" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
  <DataTypes />
  <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
  {_aoi_definitions()}
  <Tags>{''.join(_all_tags(defective))}</Tags>
  <Programs>{''.join(programs)}</Programs>
  <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>{scheduled}</ScheduledPrograms></Task></Tasks>
</Controller>
</RSLogix5000Content>'''


def requirements_payload() -> dict[str, Any]:
    items = [
        {"id": "REQ-D01", "text": "IF AutoMode=TRUE AND EStopHealthy=TRUE AND CV017_Ready=TRUE AND CV018_Ready=FALSE THEN CV017_RunCmd=FALSE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D02", "text": "CV010 jam detection shall not assert before 2000 ms of continuous blocked-photoeye running condition.", "verification_mode": "DYNAMIC", "criticality": "MEDIUM"},
        {"id": "REQ-D03", "text": "The FAT plan shall include a negative test for CV005_PE_In stuck ON while CV005 is running.", "verification_mode": "DYNAMIC", "criticality": "MEDIUM"},
        {"id": "REQ-D04", "text": "IF CV021_VFDReady=FALSE THEN CV021_RunCmd=FALSE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D05", "text": "IF CV022_VFDFault=TRUE THEN CV022_RunCmd=FALSE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D06", "text": "Merge01 shall never assert Merge01_CV011_Release and Merge01_CV012_Release simultaneously.", "verification_mode": "DYNAMIC", "criticality": "HIGH"},
        {"id": "REQ-D07", "text": "IF PackageDestination > 2 AND PackageDestination < 4 AND PackagePresent=TRUE AND CH03_Full=FALSE THEN DIV03_Fire=TRUE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D08", "text": "TrackingIndex shall advance by exactly one slot for each EncoderPulse.", "verification_mode": "DYNAMIC", "criticality": "HIGH"},
        {"id": "REQ-D09", "text": "IF CH07_Full=TRUE THEN DIV07_Fire=FALSE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D10", "text": "IF FaultResetPB=TRUE AND EStopHealthy=FALSE THEN MasterFault=TRUE", "verification_mode": "STATIC", "criticality": "CRITICAL"},
        {"id": "REQ-D11", "text": "CV011_JamTimer shall reset when CV011_PE_In becomes FALSE.", "verification_mode": "STATIC", "criticality": "MEDIUM"},
        {"id": "REQ-D16", "text": "A tracked package that becomes missing shall assert a lost-package fault after 3000 ms.", "verification_mode": "DYNAMIC", "criticality": "HIGH"},
        {"id": "REQ-D17", "text": "IF ManualMode=TRUE AND CV027_ManualPB=TRUE AND EStopHealthy=FALSE THEN CV027_RunCmd=FALSE", "verification_mode": "STATIC", "criticality": "CRITICAL"},
        {"id": "REQ-D18", "text": "IF CV005_VFDFault=TRUE THEN CV005_FaultLatched=TRUE", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D19", "text": "IF BarcodeValid=FALSE THEN PackageDestination=0", "verification_mode": "STATIC", "criticality": "HIGH"},
        {"id": "REQ-D20", "text": "IF PackagePresent=FALSE THEN DIV08_Fire=FALSE", "verification_mode": "STATIC", "criticality": "HIGH"},
    ]
    return {"schema": "devagent-plc-requirements-v1", "requirements": items}


def manifest_payload() -> dict[str, Any]:
    return {
        "schema": BENCHMARK_SCHEMA,
        "benchmark": BENCHMARK_NAME,
        "equipment": {
            "conveyors": 40,
            "diverters": 8,
            "chutes": 16,
            "photoeyes": 80,
            "vfds": 40,
            "barcode_tunnels": 1,
            "encoder_tracking_systems": 1,
        },
        "seeded_defects": list(DEFECTS),
        "acceptance_targets": {
            "critical_defect_recall": 1.0,
            "high_defect_recall": 0.95,
            "overall_defect_recall": 0.90,
            "false_verified_defects": 0,
            "common_rll_instruction_coverage": 0.95,
            "supported_st_coverage": 0.80,
            "branch_semantic_coverage": 0.80,
            "release_without_runtime_evidence": "NOT_READY",
        },
        "required_test_classes": [
            "NORMAL_START", "DOWNSTREAM_BLOCKED", "VFD_NOT_READY", "VFD_FAULT", "JAM", "PHOTOEYE_STUCK_ON",
            "MERGE_SIMULTANEOUS_REQUEST", "CHUTE_FULL", "BARCODE_NO_READ", "LOST_PACKAGE", "FAULT_RESET_BLOCKED",
            "MANUAL_MODE_SAFETY", "DIVERTER_WITHOUT_PACKAGE",
        ],
    }


def generate_warehouse_benchmark(output_dir: Path) -> dict[str, Path]:
    root = output_dir.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "baseline": root / f"{BENCHMARK_NAME}_Baseline.L5X",
        "defective": root / f"{BENCHMARK_NAME}.L5X",
        "requirements": root / "requirements.json",
        "manifest": root / "benchmark_manifest.json",
        "seeded_defects": root / "seeded_defects.json",
    }
    files["baseline"].write_text(build_l5x(defective=False), encoding="utf-8")
    files["defective"].write_text(build_l5x(defective=True), encoding="utf-8")
    files["requirements"].write_text(json.dumps(requirements_payload(), indent=2) + "\n", encoding="utf-8")
    files["manifest"].write_text(json.dumps(manifest_payload(), indent=2) + "\n", encoding="utf-8")
    files["seeded_defects"].write_text(json.dumps({"defects": list(DEFECTS)}, indent=2) + "\n", encoding="utf-8")
    return files


def _requirement_signal(result, requirement_id: str) -> bool:
    verification = next((item for item in result.requirement_verification if item.requirement_id == requirement_id), None)
    if verification is None:
        return False
    return verification.status in {
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        RequirementStatus.CONFLICT,
        RequirementStatus.NOT_MAPPED,
    }


def _risk_signal(result, subject: str) -> bool:
    folded = subject.casefold()
    return any(
        folded in " ".join((risk.title, risk.summary, *risk.evidence_ids)).casefold()
        for risk in result.risks
    )


def _regression_signal(result, subject: str) -> bool:
    folded = subject.casefold()
    return any(
        folded in change.subject.casefold()
        or any(folded in tag.casefold() for tag in change.affected_tags)
        for change in result.regression_changes
    )


def score_warehouse_benchmark(
    defective_project: Path,
    *,
    baseline_project: Path,
    requirements_path: Path,
) -> dict[str, Any]:
    result = run_production_verification_v5(
        defective_project,
        requirement_paths=[requirements_path],
        baseline_path=baseline_project,
    )
    verification_by_id = {item.requirement_id: item for item in result.requirement_verification}
    defect_results: list[dict[str, Any]] = []
    false_verified: list[str] = []

    for defect in DEFECTS:
        req_id = defect.get("requirement_id")
        detected = False
        signals: list[str] = []
        if req_id:
            verification = verification_by_id.get(str(req_id))
            if verification is not None and verification.status in {
                RequirementStatus.STATICALLY_VERIFIED,
                RequirementStatus.DYNAMICALLY_VERIFIED,
            }:
                false_verified.append(str(defect["id"]))
            if _requirement_signal(result, str(req_id)):
                detected = True
                signals.append("REQUIREMENT_GAP")
        if _risk_signal(result, str(defect["subject"])):
            detected = True
            signals.append("RISK")
        if _regression_signal(result, str(defect["subject"])):
            detected = True
            signals.append("REGRESSION")
        defect_results.append({**defect, "detected": detected, "signals": signals})

    total = len(defect_results)
    detected_total = sum(1 for item in defect_results if item["detected"])
    critical = [item for item in defect_results if item["severity"] == "CRITICAL"]
    high = [item for item in defect_results if item["severity"] == "HIGH"]

    def recall(items: list[dict[str, Any]]) -> float:
        return 1.0 if not items else sum(1 for item in items if item["detected"]) / len(items)

    return {
        "schema": "devagent-rockwell-warehouse-benchmark-score-v1",
        "benchmark": BENCHMARK_NAME,
        "project_sha256": result.engineering.project.metadata.source_sha256,
        "baseline_sha256": result.baseline_sha256,
        "inventory": {
            "tags": len(result.engineering.project.tags),
            "programs": len(result.engineering.project.programs),
            "routines": len(result.engineering.project.routines),
            "rll_rungs": len(result.engineering.project.rungs),
            "st_statements": result.engineering.project.st_statement_total,
            "aois": len(result.engineering.project.aois),
        },
        "coverage": {
            "instruction": result.engineering.project.instruction_semantic_coverage,
            "branch": result.engineering.project.branch_semantic_coverage,
            "structured_text": result.engineering.project.st_semantic_coverage,
            "aoi_body": result.engineering.project.aoi_internal_coverage,
            "aoi_call": result.engineering.project.aoi_call_coverage,
        },
        "defects": defect_results,
        "metrics": {
            "seeded_defects": total,
            "detected": detected_total,
            "overall_recall": detected_total / total,
            "critical_recall": recall(critical),
            "high_recall": recall(high),
            "false_verified_defects": false_verified,
            "requirements_total": len(result.requirements),
            "fat_tests": len(result.engineering.fat_tests),
            "regression_changes": len(result.regression_changes),
            "risks": len(result.risks),
            "readiness": result.readiness.status.value if result.readiness else None,
        },
    }


def write_score(report_path: Path, score: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "BENCHMARK_NAME",
    "BENCHMARK_SCHEMA",
    "DEFECTS",
    "build_l5x",
    "generate_warehouse_benchmark",
    "manifest_payload",
    "requirements_payload",
    "score_warehouse_benchmark",
    "sha256",
    "write_score",
]
