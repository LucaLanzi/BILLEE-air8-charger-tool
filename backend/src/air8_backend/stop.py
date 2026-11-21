"""Stopping a running job.

No Bluetooth "stop" command for the Air8 has been published. Two ways to stop:

1. **Captured stop command** (preferred once known). Capture the bytes the ISDLink
   app sends when you press Stop (README: "Capturing the real stop command"),
   then give them to the backend with ``--stop-hex``, the ``AIR8_STOP_HEX``
   environment variable, or the ``stop_command`` file in the config folder.
   Format: ``"13 xx 00 ..."`` (sent on AF01) or ``"af02: ..."``.

2. **Soft stop** (default). Replaces the running job with one that ends almost
   immediately, using the job-start command that is confirmed to work on the
   Air8: charge at the minimum current (100 mA) to the lowest allowed voltage,
   with a 1 mAh capacity limit. Worst case it adds 1 mAh at 100 mA (about 36 s).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from . import protocol as p
from .limits import CHEMISTRIES, AIR8_MIN_CURRENT_MA

SOFT_STOP_CAPACITY_MAH = 1
CONFIRM_TIMEOUT_S = 8.0
STOPPED_CURRENT_MA = 150  # at or below this, the pack is no longer being worked


def parse_stop_hex(text: Optional[str]) -> Optional[tuple[str, bytes]]:
    """Parse ``"13 ab 00"`` or ``"af02: 13 ab 00"`` into (characteristic, bytes)."""
    if not text or not text.strip():
        return None
    text = text.strip()
    char = "af01"
    if ":" in text:
        head, text = text.split(":", 1)
        char = head.strip().lower()
        if char not in ("af01", "af02"):
            raise ValueError(f"stop command characteristic must be af01 or af02, not '{head}'")
    data = bytes.fromhex(text.replace(",", " "))
    if not 1 <= len(data) <= 64:
        raise ValueError("stop command must be 1-64 bytes")
    return char, data


def load_stop_command(cli_value: Optional[str], config_dir) -> Optional[tuple[str, bytes]]:
    """CLI flag beats environment variable beats config file."""
    for source in (cli_value, os.environ.get("AIR8_STOP_HEX")):
        if source:
            return parse_stop_hex(source)
    f = config_dir / "stop_command"
    if f.exists():
        return parse_stop_hex(f.read_text())
    return None


def soft_stop_task(battery_type: Optional[int], cells: Optional[int]) -> p.WorkTask:
    """A job that finishes almost at once, for the pack that is connected."""
    name = p.BATTERY_TYPE_NAMES.get(battery_type) if battery_type is not None else None
    chem = CHEMISTRIES.get(name or "LiPo", CHEMISTRIES["LiPo"])
    return p.WorkTask(
        task_type=p.TASK_TYPES["charge"],
        battery_type=p.BATTERY_TYPES[chem.name],
        current_ma=AIR8_MIN_CURRENT_MA,
        cells=cells if cells and cells <= chem.max_cells else 0,
        cutoff_mv=chem.charge_min_mv,
        capacity_limit_mah=SOFT_STOP_CAPACITY_MAH,
    )


def _is_stopped(latest: dict) -> bool:
    e = latest.get("electric") or {}
    w = latest.get("work_state") or {}
    if w.get("state_label") in ("idle", "done"):
        return True
    return e.get("charge_ma") is not None and e["charge_ma"] <= STOPPED_CURRENT_MA


async def stop_job(charger, stop_cmd: Optional[tuple[str, bytes]]) -> dict:
    """Stop whatever is running and report whether the charger confirms it."""
    ws = charger.latest.get("work_state") or {}
    last = (charger.last_job or {}).get("task") or {}
    battery_type = ws.get("battery_type", last.get("battery_type"))
    cells = ws.get("cells") or last.get("cells")

    ack = None
    if stop_cmd is not None:
        method = "captured"
        await charger.send_raw(*stop_cmd)
    else:
        method = "soft"
        ack = (await charger.start_job(soft_stop_task(battery_type, cells)))["ack"]
        if ack is not None and not ack["ok"]:
            return {"method": method, "ack": ack, "confirmed": False}

    # Wait for fresh telemetry showing the pack is no longer being worked.
    start = time.monotonic()
    seen = id(charger.latest.get("electric"))
    while time.monotonic() - start < CONFIRM_TIMEOUT_S:
        await asyncio.sleep(0.5)
        fresh = id(charger.latest.get("electric")) != seen
        if fresh and _is_stopped(charger.latest):
            return {"method": method, "ack": ack, "confirmed": True}
    return {"method": method, "ack": ack, "confirmed": False}
