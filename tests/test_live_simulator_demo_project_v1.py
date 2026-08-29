from __future__ import annotations

from pathlib import Path

from devagent.plc import analyze_rockwell_l5x


PROJECT = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "live"
    / "warehouse_commissioning_demo.L5X"
)


def test_demo_project_parses_as_full_rockwell_engineering_context() -> None:
    engineering = analyze_rockwell_l5x(PROJECT)
    project = engineering.project
    assert project.metadata.vendor.casefold().startswith("rockwell")
    assert project.metadata.controller_name == "WarehouseCommissioningDemo"
    names = {tag.name for tag in project.tags}
    assert {
        "AutoMode",
        "StartRequest",
        "SafetyOK",
        "SafetyTrip",
        "DriveReady",
        "DriveFault",
        "DownstreamReady",
        "RunCmd",
        "FaultCode",
        "MachineState",
    }.issubset(names)


def test_demo_run_cmd_rule_has_known_ground_truth_conditions() -> None:
    engineering = analyze_rockwell_l5x(PROJECT)
    rules = [rule for rule in engineering.project.output_logic if rule.output_tag == "RunCmd"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule.instruction == "OTE"
    assert len(rule.paths) == 1
    terms = {term.tag: term.required for term in rule.paths[0].terms}
    assert terms == {
        "AutoMode": True,
        "StartRequest": True,
        "SafetyOK": True,
        "SafetyTrip": False,
        "DriveReady": True,
        "DriveFault": False,
        "DownstreamReady": True,
    }
