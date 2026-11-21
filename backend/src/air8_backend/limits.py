"""Per-chemistry limits and job validation.

The charger enforces its own limits too, but a bad packet should never leave
this program in the first place. Voltages here are per cell, in mV.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .protocol import BATTERY_TYPES, TASK_TYPES, WorkTask

AIR8_MAX_CURRENT_MA = 20_000
AIR8_MIN_CURRENT_MA = 100
AIR8_MAX_POWER_W = 500


@dataclass(frozen=True)
class Chemistry:
    name: str
    max_cells: int
    # charge target voltage (per cell)
    charge_min_mv: int
    charge_max_mv: int
    charge_default_mv: int
    # discharge end voltage (per cell)
    discharge_min_mv: int
    discharge_max_mv: int
    discharge_default_mv: int
    # storage voltage (per cell); None = storage mode not available
    storage_default_mv: Optional[int]
    # NiMH/NiCd charge cut-off is a delta-peak in mV, not a target voltage
    delta_peak: bool = False


CHEMISTRIES: dict[str, Chemistry] = {
    "LiPo":  Chemistry("LiPo", 8, 3850, 4200, 4200, 3000, 3800, 3500, 3850),
    "LiHV":  Chemistry("LiHV", 8, 3850, 4350, 4350, 3000, 3800, 3500, 3850),
    "ULiHV": Chemistry("ULiHV", 8, 3850, 4450, 4450, 3000, 3800, 3500, 3900),
    "LiIon": Chemistry("LiIon", 8, 3800, 4200, 4100, 2750, 3700, 3000, 3750),
    "LiFe":  Chemistry("LiFe", 8, 3300, 3650, 3600, 2500, 3200, 2800, 3300),
    "Pb":    Chemistry("Pb", 12, 2200, 2450, 2400, 1750, 2100, 1800, None),
    # NiMH charge: delta-peak 3-12 mV. Discharge: end voltage per cell.
    "NiMH":  Chemistry("NiMH", 16, 3, 12, 5, 800, 1200, 1000, None, delta_peak=True),
}


class JobError(ValueError):
    pass


@dataclass
class JobRequest:
    task: str                 # charge | discharge | storage | destroy
    chemistry: str            # key of CHEMISTRIES
    cells: int                # 0 = auto
    current_ma: int
    cutoff_mv: Optional[int] = None  # None = chemistry default for the task
    capacity_limit_mah: int = 0
    confirm_destroy: bool = False


def defaults() -> dict:
    """Everything the UI needs to render the job form."""
    return {
        "max_current_ma": AIR8_MAX_CURRENT_MA,
        "min_current_ma": AIR8_MIN_CURRENT_MA,
        "max_power_w": AIR8_MAX_POWER_W,
        "tasks": list(TASK_TYPES),
        "chemistries": {
            k: {
                "max_cells": c.max_cells,
                "delta_peak": c.delta_peak,
                "storage": c.storage_default_mv is not None,
                "charge": [c.charge_min_mv, c.charge_max_mv, c.charge_default_mv],
                "discharge": [c.discharge_min_mv, c.discharge_max_mv, c.discharge_default_mv],
                "storage_mv": c.storage_default_mv,
            }
            for k, c in CHEMISTRIES.items()
        },
    }


def validate(req: JobRequest) -> tuple[WorkTask, list[str]]:
    """Check a job against the limits. Returns the packet fields and warnings.

    Raises JobError for anything that must not be sent.
    """
    warnings: list[str] = []

    if req.task not in TASK_TYPES:
        raise JobError(f"unknown task '{req.task}'")
    chem = CHEMISTRIES.get(req.chemistry)
    if chem is None:
        raise JobError(f"unknown chemistry '{req.chemistry}'")

    if not (0 <= req.cells <= chem.max_cells):
        raise JobError(f"{chem.name} supports 1-{chem.max_cells} cells (0 = auto)")
    if req.cells == 0:
        warnings.append("Cell count is auto-detect. Check the detected count on the charger before leaving it.")

    if not (AIR8_MIN_CURRENT_MA <= req.current_ma <= AIR8_MAX_CURRENT_MA):
        raise JobError(f"current must be {AIR8_MIN_CURRENT_MA}-{AIR8_MAX_CURRENT_MA} mA")

    if not (0 <= req.capacity_limit_mah <= 1_000_000):
        raise JobError("capacity limit must be 0 (off) to 1,000,000 mAh")

    cutoff = req.cutoff_mv
    if req.task == "charge":
        lo, hi, dflt = chem.charge_min_mv, chem.charge_max_mv, chem.charge_default_mv
        unit = "mV delta-peak" if chem.delta_peak else "mV/cell"
    elif req.task == "storage":
        if chem.storage_default_mv is None:
            raise JobError(f"storage mode is not available for {chem.name}")
        lo, hi, dflt = chem.discharge_max_mv, chem.charge_max_mv, chem.storage_default_mv
        unit = "mV/cell"
    elif req.task == "discharge":
        lo, hi, dflt = chem.discharge_min_mv, chem.discharge_max_mv, chem.discharge_default_mv
        unit = "mV/cell"
    else:  # destroy
        if not req.confirm_destroy:
            raise JobError("Destroy fully drains the pack for disposal. Tick the confirmation box to proceed.")
        lo, hi, dflt = 0, chem.discharge_max_mv, 0
        unit = "mV/cell"
        warnings.append("DESTROY mode: the pack will be unusable afterwards.")

    if cutoff is None:
        cutoff = dflt
    if not (lo <= cutoff <= hi):
        raise JobError(f"{req.task} cut-off for {chem.name} must be {lo}-{hi} {unit}")

    if req.task in ("charge", "storage") and req.cells and not chem.delta_peak:
        watts = req.current_ma / 1000 * req.cells * (cutoff / 1000)
        if watts > AIR8_MAX_POWER_W:
            warnings.append(
                f"{watts:.0f} W requested exceeds the Air8's {AIR8_MAX_POWER_W} W; "
                "the charger will reduce the current."
            )
    if req.task in ("discharge", "destroy"):
        warnings.append("The charger limits discharge power internally; actual current may be lower.")

    task = WorkTask(
        task_type=TASK_TYPES[req.task],
        battery_type=BATTERY_TYPES[chem.name],
        current_ma=req.current_ma,
        cells=req.cells,
        cutoff_mv=cutoff,
        capacity_limit_mah=req.capacity_limit_mah,
    )
    return task, warnings
