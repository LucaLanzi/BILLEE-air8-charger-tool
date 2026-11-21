"""ISDT Air8 Bluetooth LE protocol: packet builders and parsers.

Pure functions only (no I/O), so everything here is unit-testable.

The protocol is not published by ISDT. Everything below comes from two
MIT-licensed community reverse-engineering projects:

  * mtheli/isdt_air_ble          (Home Assistant integration, read side)
  * DittelHome/ISDT-Charge-Utility (desktop GUI, adds the 0xEA work-task write)

Read-side commands (hardware info, electrical telemetry, work state) are
used by those projects against real chargers. The 0xEA "work task" write
that starts a charge job is documented for the Air8 but has NOT been
independently verified on an Air8 by either project. Treat it as
experimental until you've confirmed it on the bench.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, asdict
from typing import Optional

# --------------------------------------------------------------------------
# GATT layout
# --------------------------------------------------------------------------
SERVICE_UUID = "0000af00-0000-1000-8000-00805f9b34fb"
AF01_UUID = "0000af01-0000-1000-8000-00805f9b34fb"  # polling + control (notify/write)
AF02_UUID = "0000af02-0000-1000-8000-00805f9b34fb"  # bind, hw info, stream (notify/write)

# Advertisement manufacturer data: company id 0xABBA, bytes [2:6] identify model.
ISDT_COMPANY_ID = 0xABBA
AIR8_MODEL_BYTES = bytes([0x01, 0x03, 0x00, 0x00])

RESPONSE_HEADER = 0x31  # first byte of every AF01 response

# Air8 battery-type codes (differ from the C4/A4/A8 "standard" table!)
BATTERY_TYPES: dict[str, int] = {
    "LiHV": 0,
    "LiPo": 1,
    "LiIon": 2,
    "LiFe": 3,
    "Pb": 4,
    "NiMH": 5,  # NiMH / NiCd
    "ULiHV": 6,
}
BATTERY_TYPE_NAMES = {v: k for k, v in BATTERY_TYPES.items()}

TASK_TYPES: dict[str, int] = {
    "charge": 0,
    "discharge": 1,
    "storage": 2,
    "destroy": 3,  # full discharge before disposal
}
TASK_TYPE_NAMES = {v: k for k, v in TASK_TYPES.items()}

# Air8 work-state codes (it uses a different enum than the C4/A4 round-cell
# chargers). Mapping from mtheli/isdt_air_ble's AIR8_WORK_STATE_MAP, which
# only distinguishes "working" from "done"; the UI also shows the raw code.
_WORKING = {3, 4, 5, 7, 8, 9, 10, 12, 14, 16, 17, 18}
_DONE = {6, 11, 13, 15}
WORK_STATE_LABELS = {0: "idle"}
WORK_STATE_LABELS.update({s: "working" for s in _WORKING})
WORK_STATE_LABELS.update({s: "done" for s in _DONE})


# --------------------------------------------------------------------------
# Request builders
# --------------------------------------------------------------------------
def hw_info_req() -> bytes:
    """AF02: ask for hardware/firmware version and device id."""
    return bytes([0xE0])


def bind_req(client_id: bytes) -> bytes:
    """AF02: bind this client. ``client_id`` is a persistent 16-byte id."""
    if len(client_id) != 16:
        raise ValueError("client_id must be 16 bytes")
    return bytes([0x18]) + client_id + b"\x00\x00"


def start_stream_req() -> bytes:
    """AF02: start the data stream (sent after binding)."""
    return bytes([0xE2])


def electric_req(channel: int = 0) -> bytes:
    """AF01: input/output voltage, current, per-cell voltages."""
    return bytes([0x12, 0xE4, channel & 0xFF])


def work_state_req(channel: int = 0) -> bytes:
    """AF01: current job state, capacity, energy, settings."""
    return bytes([0x13, 0xE6, channel & 0xFF])


def ir_req(channel: int = 0) -> bytes:
    """AF01: per-cell internal resistance (documented for C4/A4, may not answer on Air8)."""
    return bytes([0x13, 0xFA, channel & 0xFF])


def alarm_tone_req() -> bytes:
    return bytes([0x12, 0x92])


def alarm_tone_set(enabled: bool) -> bytes:
    return bytes([0x13, 0x9C, 1 if enabled else 0])


@dataclass
class WorkTask:
    """Everything the charger accepts for a job (mirrors the on-device menu)."""

    task_type: int          # TASK_TYPES value
    battery_type: int       # BATTERY_TYPES value
    current_ma: int         # charge or discharge current
    cells: int              # 0 = auto-detect
    cutoff_mv: int          # per-cell target voltage, or NiMH delta-peak in mV
    capacity_limit_mah: int = 0  # 0 = unlimited
    channel: int = 0
    link_type: int = 0


def work_task_req(task: WorkTask) -> bytes:
    """AF01: 17-byte 0xEA packet that starts a job with the given settings.

    Layout: 13 EA ch task batt link cur(u32 LE) cells cutoff(u16 LE) cap(u32 LE)
    """
    pkt = struct.pack(
        "<BBBBBBIBHI",
        0x13,
        0xEA,
        task.channel & 0xFF,
        task.task_type & 0xFF,
        task.battery_type & 0xFF,
        task.link_type & 0xFF,
        task.current_ma,
        task.cells & 0xFF,
        task.cutoff_mv,
        task.capacity_limit_mah,
    )
    assert len(pkt) == 17
    return pkt


# --------------------------------------------------------------------------
# Response parsers
# --------------------------------------------------------------------------
@dataclass
class HwInfo:
    hw_version: str
    fw_version: str
    device_id: int


@dataclass
class Electric:
    channel: int
    input_mv: int
    input_ma: int
    output_mv: int
    charge_ma: int
    cell_mv: list[int] = field(default_factory=list)


@dataclass
class WorkState:
    channel: int
    state: int
    state_label: str
    progress_pct: Optional[int] = None
    capacity_mah: Optional[int] = None
    energy_mwh: Optional[int] = None
    elapsed_ms: Optional[int] = None
    battery_type: Optional[int] = None
    battery_name: Optional[str] = None
    cells: Optional[int] = None
    link_type: Optional[int] = None
    cutoff_mv: Optional[int] = None
    current_ma: Optional[int] = None
    min_input_mv: Optional[int] = None
    max_output_mw: Optional[int] = None
    error_code: Optional[int] = None


@dataclass
class TaskAck:
    channel: int
    ok: bool
    raw_status: int


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def parse_hw_info(b: bytes) -> Optional[HwInfo]:
    if len(b) < 13 or b[0] != 0xE1:
        return None
    return HwInfo(
        hw_version=f"{b[1]}.{b[2]}",
        fw_version=f"{b[3]}.{b[4]}",
        device_id=struct.unpack_from("<Q", b, 5)[0],
    )


def parse_bind_resp(b: bytes) -> Optional[bool]:
    """Returns True on success, False on failure, None if not a bind response."""
    if len(b) < 3 or b[0] != 0x19:
        return None
    return b[2] == 0


def parse_electric(b: bytes) -> Optional[Electric]:
    if len(b) < 15 or b[0] != RESPONSE_HEADER or b[1] != 0xE5:
        return None
    ch = b[2]
    if len(b) > 35:  # long format: 32-bit voltages, up to 16 cells
        e = Electric(ch, _u32(b, 3), _u32(b, 7), _u32(b, 11), _u32(b, 15))
        cell_off, max_cells = 19, 16
    else:  # short format: 16-bit voltages, up to 8 cells
        e = Electric(ch, _u16(b, 3), _u32(b, 5), _u16(b, 9), _u32(b, 11))
        cell_off, max_cells = 15, 8
    for i in range(max_cells):
        off = cell_off + 2 * i
        if off + 2 > len(b):
            break
        e.cell_mv.append(_u16(b, off))
    # trailing zeroes are unused cell slots
    while e.cell_mv and e.cell_mv[-1] == 0:
        e.cell_mv.pop()
    return e


def parse_work_state(b: bytes) -> Optional[WorkState]:
    if len(b) < 4 or b[0] != RESPONSE_HEADER or b[1] != 0xE7:
        return None
    n = len(b)
    ws = WorkState(channel=b[2], state=b[3], state_label=WORK_STATE_LABELS.get(b[3], "unknown"))

    def has(off: int, size: int) -> bool:
        return off + size <= n

    if has(4, 1):
        ws.progress_pct = b[4]
    if has(5, 4):
        ws.capacity_mah = _u32(b, 5)
    if has(9, 4):
        ws.energy_mwh = _u32(b, 9)
    if has(13, 4):
        ws.elapsed_ms = _u32(b, 13)
    if has(17, 1):
        ws.battery_type = b[17]
        ws.battery_name = BATTERY_TYPE_NAMES.get(b[17])
    if has(18, 1):
        ws.cells = b[18]
    if has(19, 1):
        ws.link_type = b[19]
    if has(20, 2):
        ws.cutoff_mv = _u16(b, 20)
    if has(22, 4):
        ws.current_ma = _u32(b, 22)
    if has(30, 2):
        ws.min_input_mv = _u16(b, 30)
    if has(32, 4):
        ws.max_output_mw = _u32(b, 32)
    if has(36, 2):
        ws.error_code = _u16(b, 36)
    return ws


def parse_ir(b: bytes) -> Optional[dict]:
    """Per-cell internal resistance in milliohms (None for invalid readings)."""
    if len(b) < 3 or b[0] != RESPONSE_HEADER or b[1] != 0xFB:
        return None
    vals = []
    for off in range(3, len(b) - 1, 2):
        raw = _u16(b, off)
        vals.append(None if raw == 0 or raw >= 10000 else raw / 10.0)
    while vals and vals[-1] is None:
        vals.pop()
    return {"channel": b[2], "cell_mohm": vals}


def parse_task_ack(b: bytes) -> Optional[TaskAck]:
    if len(b) < 4 or b[0] != RESPONSE_HEADER or b[1] != 0xEB:
        return None
    return TaskAck(channel=b[2], ok=b[3] == 0, raw_status=b[3])


def parse_any(b: bytes) -> Optional[tuple[str, dict]]:
    """Dispatch a notification to the right parser. Returns (kind, data)."""
    for kind, fn in (
        ("hw_info", parse_hw_info),
        ("electric", parse_electric),
        ("work_state", parse_work_state),
        ("ir", parse_ir),
        ("task_ack", parse_task_ack),
    ):
        r = fn(b)
        if r is not None:
            return kind, (asdict(r) if hasattr(r, "__dataclass_fields__") else r)
    bound = parse_bind_resp(b)
    if bound is not None:
        return "bind", {"ok": bound}
    return None


def is_air8_advertisement(manufacturer_data: dict[int, bytes]) -> bool:
    data = manufacturer_data.get(ISDT_COMPANY_ID)
    return bool(data) and len(data) >= 6 and data[2:6] == AIR8_MODEL_BYTES
