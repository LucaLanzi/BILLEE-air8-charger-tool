"""Charger connection: a real BLE link (bleak) and a simulator with the same API."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from . import protocol as p

log = logging.getLogger("air8")

COMMAND_INTERVAL_S = 0.2   # spacing between GATT writes (DittelHome uses 0.2 s)
RESPONSE_TIMEOUT_S = 1.5
POLL_PERIOD_S = 1.0        # one full telemetry cycle per second
BIND_TIMEOUT_S = 10.0


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "billee-air8"
    d.mkdir(parents=True, exist_ok=True)
    return d


def client_id() -> bytes:
    """Persistent 16-byte id used to bind with the charger (reused across runs)."""
    f = _config_dir() / "client_id"
    if f.exists():
        try:
            b = bytes.fromhex(f.read_text().strip())
            if len(b) == 16:
                return b
        except ValueError:
            pass
    b = uuid.uuid4().bytes
    f.write_text(b.hex())
    return b


class EventBus:
    """Fan-out of events to every connected websocket."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        msg = {"type": kind, "t": time.time(), "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow client; drop


class ChargerBase:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.connected = False
        self.address: Optional[str] = None
        self.name: Optional[str] = None
        self.hw_info: Optional[dict] = None
        self.bound: Optional[bool] = None
        self.latest: dict[str, Any] = {}
        self.last_job: Optional[dict] = None

    def state(self) -> dict:
        return {
            "connected": self.connected,
            "address": self.address,
            "name": self.name,
            "hw_info": self.hw_info,
            "bound": self.bound,
            "latest": self.latest,
            "last_job": self.last_job,
            "simulated": isinstance(self, SimCharger),
        }

    def _status(self) -> None:
        self.bus.publish("status", self.state())

    # API every implementation provides
    async def scan(self, timeout: float = 5.0) -> list[dict]: ...
    async def connect(self, address: str) -> None: ...
    async def disconnect(self) -> None: ...
    async def start_job(self, task: p.WorkTask) -> dict: ...
    async def set_alarm(self, enabled: bool) -> None: ...
    async def send_raw(self, char: str, data: bytes) -> None: ...


# --------------------------------------------------------------------------
# Real hardware
# --------------------------------------------------------------------------
class BleCharger(ChargerBase):
    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus)
        self._client = None
        self._poll_task: Optional[asyncio.Task] = None
        self._waiters: dict[str, asyncio.Future] = {}
        self._io_lock = asyncio.Lock()
        self._ir_supported: Optional[bool] = None
        self._write_response: dict[str, bool] = {}  # char uuid -> use write-with-response
        self._rx_count = 0

    async def scan(self, timeout: float = 5.0) -> list[dict]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for dev, adv in found.values():
            is_isdt = p.ISDT_COMPANY_ID in adv.manufacturer_data or p.SERVICE_UUID in [
                u.lower() for u in adv.service_uuids
            ]
            if not is_isdt:
                continue
            out.append({
                "address": dev.address,
                "name": dev.name or adv.local_name or "ISDT device",
                "rssi": adv.rssi,
                "is_air8": p.is_air8_advertisement(adv.manufacturer_data),
            })
        out.sort(key=lambda d: (not d["is_air8"], -(d["rssi"] or -999)))
        return out

    def _on_notify(self, _char, data: bytearray) -> None:
        b = bytes(data)
        self._rx_count += 1
        self.bus.publish("rx", b.hex(" "))
        parsed = p.parse_any(b)
        if parsed is None:
            return
        kind, payload = parsed
        if kind in ("electric", "work_state", "ir"):
            self.latest[kind] = payload
            self.bus.publish(kind, payload)
        elif kind == "hw_info":
            self.hw_info = payload
        elif kind == "bind":
            self.bound = payload["ok"]
        elif kind == "task_ack":
            self.bus.publish("task_ack", payload)
        fut = self._waiters.pop(kind, None)
        if fut and not fut.done():
            fut.set_result(payload)

    async def _write(self, char_uuid: str, data: bytes) -> None:
        self.bus.publish("tx", data.hex(" "))
        await self._client.write_gatt_char(
            char_uuid, data, response=self._write_response.get(char_uuid, False))

    def _inspect_characteristics(self) -> None:
        """Pick the write type each characteristic actually supports, and log it.

        Sending write-without-response to a characteristic that only accepts
        normal writes is silently dropped on macOS, so this must match.
        """
        services = self._client.services
        found_any = False
        for uuid_ in (p.AF01_UUID, p.AF02_UUID):
            char = services.get_characteristic(uuid_)
            if char is None:
                self.bus.publish("log", f"Characteristic {uuid_[4:8].upper()} not found on this device.")
                continue
            found_any = True
            props = list(char.properties)
            use_response = "write-without-response" not in props
            self._write_response[uuid_] = use_response
            self.bus.publish(
                "log",
                f"{uuid_[4:8].upper()}: {', '.join(props)} -> using "
                f"{'write-with-response' if use_response else 'write-without-response'}",
            )
        if not found_any:
            listing = "; ".join(
                f"{s.uuid}: " + ", ".join(f"{c.uuid}[{'/'.join(c.properties)}]" for c in s.characteristics)
                for s in services
            )
            self.bus.publish("log", f"ISDT service not found. Device GATT table: {listing}")

    async def _request(self, char_uuid: str, data: bytes, expect: str,
                       timeout: float = RESPONSE_TIMEOUT_S) -> Optional[dict]:
        """Send one command and wait for its reply. One command in flight at a time."""
        async with self._io_lock:
            fut = asyncio.get_running_loop().create_future()
            self._waiters[expect] = fut
            try:
                await self._write(char_uuid, data)
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._waiters.pop(expect, None)
                await asyncio.sleep(COMMAND_INTERVAL_S)

    async def connect(self, address: str) -> None:
        from bleak import BleakClient

        await self.disconnect()
        self.bus.publish("log", f"Connecting to {address}...")
        client = BleakClient(address, timeout=15.0, disconnected_callback=self._on_disconnect)
        await client.connect()
        self._client = client
        self.address = address
        self.name = getattr(client, "name", None) or address

        # BlueZ needs an explicit MTU exchange or long telemetry frames get cut off.
        backend = getattr(client, "_backend", None)
        if backend is not None and hasattr(backend, "_acquire_mtu"):
            try:
                await backend._acquire_mtu()
            except Exception as e:  # noqa: BLE001
                log.warning("MTU exchange failed: %s", e)
        await asyncio.sleep(1.0)
        self._rx_count = 0
        self._inspect_characteristics()

        await client.start_notify(p.AF01_UUID, self._on_notify)
        await asyncio.sleep(0.5)
        await client.start_notify(p.AF02_UUID, self._on_notify)
        await asyncio.sleep(0.5)

        # Bind first, then ask for hardware info (same order as the
        # Home Assistant integration, which is known to work with the Air8).
        bind = await self._request(p.AF02_UUID, p.bind_req(client_id()), "bind", BIND_TIMEOUT_S)
        self.bound = None if bind is None else bind["ok"]
        if self.bound is False:
            self.bus.publish("log", "Charger REJECTED the bind request.")
        elif self.bound is None:
            self.bus.publish("log", "No bind reply from charger after "
                                    f"{BIND_TIMEOUT_S:.0f} s (continuing; some firmware doesn't reply).")
        self.hw_info = await self._request(p.AF02_UUID, p.hw_info_req(), "hw_info", 3.0)
        await self._write(p.AF02_UUID, p.start_stream_req())
        await asyncio.sleep(0.2)
        if self._rx_count == 0:
            self.bus.publish(
                "log",
                "WARNING: the charger has not sent ANY data since connecting. Commands are not "
                "reaching it, or it is ignoring this client. Make sure no phone has ISDLink open, "
                "power-cycle the charger, then reconnect. Tick 'Show raw packets' and send the log.",
            )

        self.connected = True
        self._status()
        self.bus.publish("log", f"Connected. HW {self.hw_info}")
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _on_disconnect(self, _client) -> None:
        if self.connected:
            self.connected = False
            self.bus.publish("log", "Charger disconnected.")
            self._status()

    async def _poll_loop(self) -> None:
        misses = 0
        while self.connected:
            start = time.monotonic()
            try:
                e = await self._request(p.AF01_UUID, p.electric_req(0), "electric")
                w = await self._request(p.AF01_UUID, p.work_state_req(0), "work_state")
                if self._ir_supported is not False:
                    ir = await self._request(p.AF01_UUID, p.ir_req(0), "ir")
                    if self._ir_supported is None:
                        self._ir_supported = ir is not None
                misses = 0 if (e or w) else misses + 1
                if misses == 8:
                    self.bus.publish("log", "Charger stopped answering telemetry requests.")
            except Exception as ex:  # noqa: BLE001
                if not self.connected or not (self._client and self._client.is_connected):
                    break
                self.bus.publish("log", f"Poll error: {ex}")
                await asyncio.sleep(2.0)
            await asyncio.sleep(max(0.0, POLL_PERIOD_S - (time.monotonic() - start)))

    async def disconnect(self) -> None:
        self.connected = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._status()

    def _require(self) -> None:
        if not self.connected or self._client is None:
            raise RuntimeError("not connected")

    async def start_job(self, task: p.WorkTask) -> dict:
        self._require()
        ack = await self._request(p.AF01_UUID, p.work_task_req(task), "task_ack", 3.0)
        self.last_job = {"task": asdict(task), "ack": ack, "t": time.time()}
        self._status()
        return {"ack": ack}

    async def set_alarm(self, enabled: bool) -> None:
        self._require()
        async with self._io_lock:
            await self._write(p.AF01_UUID, p.alarm_tone_set(enabled))
            await asyncio.sleep(COMMAND_INTERVAL_S)

    async def send_raw(self, char: str, data: bytes) -> None:
        self._require()
        uuid_ = {"af01": p.AF01_UUID, "af02": p.AF02_UUID}[char]
        async with self._io_lock:
            await self._write(uuid_, data)
            await asyncio.sleep(COMMAND_INTERVAL_S)


# --------------------------------------------------------------------------
# Simulator (no hardware needed; for UI work and demos)
# --------------------------------------------------------------------------
class SimCharger(ChargerBase):
    ADDRESS = "SIM:AIR8:00:00"

    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus)
        self._task: Optional[asyncio.Task] = None
        self._job: Optional[p.WorkTask] = None
        self._cells = [3712, 3705, 3720, 3698, 3710, 3702]
        self._mah = 0.0
        self._mwh = 0.0
        self._t0 = 0.0
        self._state = 0

    async def scan(self, timeout: float = 5.0) -> list[dict]:
        await asyncio.sleep(0.5)
        return [{"address": self.ADDRESS, "name": "Air8 (simulated)", "rssi": -42, "is_air8": True}]

    async def connect(self, address: str) -> None:
        self.connected, self.address, self.name = True, address, "Air8 (simulated)"
        self.hw_info = {"hw_version": "1.0", "fw_version": "1.0", "device_id": 0xA1B2C3D4}
        self.bound = True
        self._status()
        self.bus.publish("log", "Connected to simulator.")
        self._task = asyncio.create_task(self._loop())

    async def disconnect(self) -> None:
        self.connected = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._status()

    async def _loop(self) -> None:
        while self.connected:
            self._step(1.0)
            await asyncio.sleep(1.0)

    def _step(self, dt: float) -> None:
        j = self._job
        current = 0
        if j and self._state == 3:
            n = len(self._cells)
            target = j.cutoff_mv if j.cutoff_mv > 500 else 4200  # NiMH delta-peak: fake a target
            sign = 1 if sum(self._cells) / n < target else -1
            gap = abs(target - sum(self._cells) / n)
            current = j.current_ma if gap > 30 else int(j.current_ma * gap / 30)  # CV taper
            for i in range(n):
                self._cells[i] += sign * (current / 1000) * dt * 0.8 + random.uniform(-1, 1)
                self._cells[i] = max(2500, min(4500, self._cells[i]))
            avg = sum(self._cells) / n
            self._mah += current * dt / 3600
            self._mwh += current * avg * n / 1000 * dt / 3600
            if gap < 3 or current < 50 or (j.capacity_limit_mah and self._mah >= j.capacity_limit_mah):
                self._state, current = 6, 0
                self.bus.publish("log", "Simulated job complete.")
        n = len(self._cells)
        out_mv = int(sum(self._cells))
        electric = {
            "channel": 0, "input_mv": 24010 + random.randint(-20, 20),
            "input_ma": int(current * out_mv / 24000 / 0.93), "output_mv": out_mv,
            "charge_ma": int(current), "cell_mv": [int(c) for c in self._cells],
        }
        ws = {
            "channel": 0, "state": self._state, "state_label": p.WORK_STATE_LABELS.get(self._state, "unknown"),
            "progress_pct": min(100, int(self._mah / 22)) if j else 0,
            "capacity_mah": int(self._mah), "energy_mwh": int(self._mwh),
            "elapsed_ms": int((time.time() - self._t0) * 1000) if j else 0,
            "battery_type": j.battery_type if j else None,
            "battery_name": p.BATTERY_TYPE_NAMES.get(j.battery_type) if j else None,
            "cells": n, "link_type": 0, "cutoff_mv": j.cutoff_mv if j else None,
            "current_ma": j.current_ma if j else None, "min_input_mv": None,
            "max_output_mw": 500_000, "error_code": 0,
        }
        self.latest.update(electric=electric, work_state=ws)
        self.bus.publish("electric", electric)
        self.bus.publish("work_state", ws)

    async def start_job(self, task: p.WorkTask) -> dict:
        if not self.connected:
            raise RuntimeError("not connected")
        self.bus.publish("tx", p.work_task_req(task).hex(" "))
        self._job, self._state, self._mah, self._mwh, self._t0 = task, 3, 0.0, 0.0, time.time()
        if task.cells:
            base = self._cells[0]
            self._cells = [base + random.uniform(-8, 8) for _ in range(task.cells)]
        ack = {"channel": 0, "ok": True, "raw_status": 0}
        self.bus.publish("task_ack", ack)
        self.last_job = {"task": asdict(task), "ack": ack, "t": time.time()}
        self._status()
        return {"ack": ack}

    async def set_alarm(self, enabled: bool) -> None:
        self.bus.publish("tx", p.alarm_tone_set(enabled).hex(" "))

    async def send_raw(self, char: str, data: bytes) -> None:
        self.bus.publish("tx", f"[{char}] {data.hex(' ')}")
        self._job, self._state = None, 0  # the simulator treats any raw command as a stop

