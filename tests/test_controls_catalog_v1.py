from __future__ import annotations

from devagent.controls.catalog import STANDARDS, get_standard


def test_company_equipment_catalog_is_explicit_and_versioned() -> None:
    assert set(STANDARDS) == {"motor-v1", "vfd-v1", "conveyor-v1", "valve-v1"}
    conveyor = get_standard("conveyor-v1")
    assert conveyor.equipment_type == "CONVEYOR"
    assert conveyor.required_commands == ("START", "STOP", "RESET")
    assert conveyor.required_status == ("READY", "RUNNING", "FAULTED")
    assert conveyor.generated_outputs == ("RUN", "RESET")


def test_valve_contract_is_not_silently_treated_as_motor() -> None:
    valve = get_standard("valve-v1")
    assert valve.required_commands == ("OPEN", "CLOSE")
    assert valve.generated_outputs == ("OPEN", "CLOSE")
